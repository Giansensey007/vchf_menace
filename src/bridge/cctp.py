from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from src.bridge.cctp_iris import poll_attestation
from src.bridge.cctp_queue import CctpClaimQueue
from src.bridge.cctp_sol import run_burn_sol, run_receive_sol, sol_usdc_ata
from src.config_loader import is_dry_run, load_bridge_config, load_chains
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.rate_limit import get_with_retry
from src.execution.tx_log import log_tx
from src.quotes.types import from_human

logger = logging.getLogger(__name__)

IRIS_FEES = "/v2/burn/USDC/fees/{source_domain}/{dest_domain}"
FAST_FINALITY_THRESHOLD = 1000


@dataclass
class CctpQuote:
    provider: str
    direction: str
    amount_in_usdc: float
    amount_out_usdc: float
    fee_usd: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.amount_out_usdc > 0


@dataclass
class CctpBridgeResult:
    direction: str
    amount_usdc: float
    source_tx: str | None
    dest_tx: str | None
    dry_run: bool
    success: bool
    error: str | None = None


def _evm_addr_bytes32(addr: str) -> bytes:
    """Left-pad an EVM address to a 32-byte CCTP mintRecipient."""
    from src.quotes.addresses import checksum

    return bytes.fromhex(checksum(addr)[2:].rjust(64, "0"))


class CircleCctpBridge:
    """Circle CCTP v2 — native USDC direct burn-and-mint between Solana, Ethereum and Base.

    CCTP is the preferred bridge for any USDC<->USDC pair: it moves native USDC directly
    A->B (no ETH transit) via the same TokenMessengerV2/MessageTransmitterV2 addresses on
    every EVM chain. Base<->Sol, ETH<->Base and ETH<->Sol are all direct.
    """

    _SUPPORTED = frozenset({"solana", "ethereum", "base"})
    _EVM = frozenset({"ethereum", "base"})

    def __init__(self) -> None:
        self.cfg = load_bridge_config()["cctp"]
        self.base = self.cfg["iris_api"].rstrip("/")

    def _domain(self, chain_key: str) -> int | None:
        key = f"{chain_key}_domain"
        return int(self.cfg[key]) if key in self.cfg else None

    async def quote_usdc(
        self,
        client: httpx.AsyncClient,
        from_chain: str,
        to_chain: str,
        amount_usdc: float,
    ) -> CctpQuote:
        direction = f"{from_chain}_to_{to_chain}"
        if (
            from_chain == to_chain
            or from_chain not in self._SUPPORTED
            or to_chain not in self._SUPPORTED
        ):
            return CctpQuote("cctp", direction, amount_usdc, 0, 0, "unsupported pair")

        src_dom = self._domain(from_chain)
        dst_dom = self._domain(to_chain)
        if src_dom is None or dst_dom is None:
            return CctpQuote("cctp", direction, amount_usdc, 0, 0, "unsupported pair")

        fee_usd = float(self.cfg.get("fee_usd_estimate", 1.5))
        try:
            url = f"{self.base}{IRIS_FEES.format(source_domain=src_dom, dest_domain=dst_dom)}"
            resp = await get_with_retry(client, url, timeout=20.0)
            if resp.status_code == 200:
                tiers = resp.json()
                if isinstance(tiers, list) and tiers:
                    fast = next(
                        (t for t in tiers if t.get("finalityThreshold") == FAST_FINALITY_THRESHOLD),
                        tiers[0],
                    )
                    fee_bps = int(fast.get("minimumFee") or 0)
                    if fee_bps:
                        fee_usd = amount_usdc * fee_bps / 10_000
        except Exception as exc:
            logger.debug("CCTP Iris fee lookup failed, using estimate: %s", exc)

        out = max(0.0, amount_usdc - fee_usd)
        return CctpQuote("cctp", direction, amount_usdc, out, fee_usd)

    def _max_fee_raw(self, fee_usd: float) -> int:
        # CCTP maxFee is in burn-token atomic units (6 decimals for USDC)
        return int(fee_usd * 1_000_000 * 1.5) + 1

    async def bridge_usdc_sol_to_eth(
        self, client: httpx.AsyncClient, amount_usdc: float
    ) -> CctpBridgeResult:
        direction = "solana_to_ethereum_usdc"
        quote = await self.quote_usdc(client, "solana", "ethereum", amount_usdc)
        if not quote.ok:
            return CctpBridgeResult(direction, amount_usdc, None, None, is_dry_run(), False, quote.error)

        if is_dry_run():
            logger.info("[DRY_RUN] CCTP USDC Sol→ETH: %.4f USDC", amount_usdc)
            return CctpBridgeResult(direction, amount_usdc, "dry-run-cctp-sol", "dry-run-cctp-eth", True, True)

        chains = load_chains()
        sol_exec = SolanaExecutor(chains["solana"])
        eth_exec = EthereumExecutor(chains["ethereum"])
        amount_raw = from_human(amount_usdc, 6)
        max_fee = self._max_fee_raw(quote.fee_usd)

        source_tx, err = run_burn_sol(
            amount_raw=amount_raw,
            max_fee_raw=max_fee,
            min_finality_threshold=int(self.cfg.get("fast_finality_threshold", FAST_FINALITY_THRESHOLD)),
            sol_rpc=chains["solana"].rpc_url,
            sol_secret=os.getenv("SOLANA_SECRET_KEY", ""),
            sol_owner=sol_exec.pubkey,
            sol_usdc_mint=self.cfg["solana_usdc"],
            eth_domain=int(self.cfg["ethereum_domain"]),
            eth_address=eth_exec.address,
            eth_usdc=self.cfg["ethereum_usdc"],
            iris_api=self.base,
        )
        if not source_tx:
            return CctpBridgeResult(direction, amount_usdc, None, None, False, False, err)

        log_tx("cctp_burn_sol_to_eth", "solana", source_tx, extra={"amount_usdc": amount_usdc})

        inline_timeout = float(os.getenv("CCTP_INLINE_POLL_SEC", "120"))
        try:
            att = await poll_attestation(
                client, int(self.cfg["solana_domain"]), source_tx, timeout_sec=inline_timeout
            )
        except Exception as exc:
            logger.warning("CCTP inline poll failed (%s), queueing claim", exc)
            att = None
        if not att:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["solana_domain"]),
                dest_domain=int(self.cfg["ethereum_domain"]),
                intent="sol_to_eth_attestation_timeout",
            )
            return CctpBridgeResult(direction, amount_usdc, source_tx, None, False, False, "attestation timeout")

        dest_tx = eth_exec.receive_message(
            message_transmitter=self.cfg["ethereum_message_transmitter"],
            message_hex=att.message,
            attestation_hex=att.attestation,
        )
        if not dest_tx:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["solana_domain"]),
                dest_domain=int(self.cfg["ethereum_domain"]),
                intent="sol_to_eth_claim_pending",
            )
            return CctpBridgeResult(
                direction, amount_usdc, source_tx, None, False, False, "ETH receiveMessage failed (queued for retry)"
            )

        log_tx("cctp_claim_eth", "ethereum", dest_tx, extra={"source_tx": source_tx})
        return CctpBridgeResult(direction, amount_usdc, source_tx, dest_tx, False, True)

    async def bridge_usdc_eth_to_sol(
        self, client: httpx.AsyncClient, amount_usdc: float
    ) -> CctpBridgeResult:
        direction = "ethereum_to_solana_usdc"
        quote = await self.quote_usdc(client, "ethereum", "solana", amount_usdc)
        if not quote.ok:
            return CctpBridgeResult(direction, amount_usdc, None, None, is_dry_run(), False, quote.error)

        if is_dry_run():
            logger.info("[DRY_RUN] CCTP USDC ETH→Sol: %.4f USDC", amount_usdc)
            return CctpBridgeResult(direction, amount_usdc, "dry-run-cctp-eth", "dry-run-cctp-sol", True, True)

        chains = load_chains()
        sol_exec = SolanaExecutor(chains["solana"])
        eth_exec = EthereumExecutor(chains["ethereum"])
        amount_raw = from_human(amount_usdc, 6)
        max_fee = self._max_fee_raw(quote.fee_usd)

        import base58

        sol_ata = sol_usdc_ata(sol_exec.pubkey, self.cfg["solana_usdc"])
        mint_recipient = base58.b58decode(sol_ata)

        source_tx = eth_exec.deposit_for_burn(
            token_messenger=self.cfg["ethereum_token_messenger"],
            usdc=self.cfg["ethereum_usdc"],
            amount=amount_raw,
            destination_domain=int(self.cfg["solana_domain"]),
            mint_recipient=mint_recipient,
            max_fee=max_fee,
            min_finality_threshold=int(self.cfg.get("fast_finality_threshold", FAST_FINALITY_THRESHOLD)),
        )
        if not source_tx:
            return CctpBridgeResult(direction, amount_usdc, None, None, False, False, "ETH depositForBurn failed")

        log_tx("cctp_burn_eth_to_sol", "ethereum", source_tx, extra={"amount_usdc": amount_usdc})

        inline_timeout = float(os.getenv("CCTP_INLINE_POLL_SEC", "120"))
        try:
            att = await poll_attestation(
                client, int(self.cfg["ethereum_domain"]), source_tx, timeout_sec=inline_timeout
            )
        except Exception as exc:
            logger.warning("CCTP inline poll failed (%s), queueing claim", exc)
            att = None
        if not att:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["ethereum_domain"]),
                dest_domain=int(self.cfg["solana_domain"]),
                intent="eth_to_sol_attestation_timeout",
            )
            return CctpBridgeResult(direction, amount_usdc, source_tx, None, False, False, "attestation timeout")

        dest_tx, err = run_receive_sol(
            message_hex=att.message,
            attestation_hex=att.attestation,
            sol_rpc=chains["solana"].rpc_url,
            sol_secret=os.getenv("SOLANA_SECRET_KEY", ""),
            sol_owner=sol_exec.pubkey,
            sol_usdc_mint=self.cfg["solana_usdc"],
            eth_domain=int(self.cfg["ethereum_domain"]),
            eth_usdc=self.cfg["ethereum_usdc"],
            iris_api=self.base,
        )
        if not dest_tx:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["ethereum_domain"]),
                dest_domain=int(self.cfg["solana_domain"]),
                intent="eth_to_sol_claim_pending",
            )
            return CctpBridgeResult(
                direction, amount_usdc, source_tx, None, False, False, err or "Sol receive failed (queued for retry)"
            )

        log_tx("cctp_claim_sol", "solana", dest_tx, extra={"source_tx": source_tx})
        return CctpBridgeResult(direction, amount_usdc, source_tx, dest_tx, False, True)

    # --- Direct Base-domain CCTP (Base<->ETH, Base<->Sol) -------------------
    # CCTP-first: prefer these over the ETH stable triangle for USDC pairs.
    # NOTE: live Base burn/mint needs validation on a real run (DRY_RUN safe here).

    def _evm_executor(self, chain_key: str, chains: dict):
        if chain_key == "base":
            from src.execution.base import BaseExecutor

            return BaseExecutor(chains["base"])
        return EthereumExecutor(chains["ethereum"])

    async def _poll_or_queue(
        self,
        client: httpx.AsyncClient,
        src_domain: int,
        dst_domain: int,
        source_tx: str,
        intent: str,
    ):
        inline_timeout = float(os.getenv("CCTP_INLINE_POLL_SEC", "120"))
        try:
            att = await poll_attestation(client, src_domain, source_tx, timeout_sec=inline_timeout)
        except Exception as exc:
            logger.warning("CCTP inline poll failed (%s), queueing claim", exc)
            att = None
        if not att:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=src_domain,
                dest_domain=dst_domain,
                intent=f"{intent}_attestation_timeout",
            )
            return None
        return att

    async def _bridge_usdc_evm_to_evm(
        self, client: httpx.AsyncClient, from_chain: str, to_chain: str, amount_usdc: float
    ) -> CctpBridgeResult:
        direction = f"{from_chain}_to_{to_chain}_usdc"
        quote = await self.quote_usdc(client, from_chain, to_chain, amount_usdc)
        if not quote.ok:
            return CctpBridgeResult(direction, amount_usdc, None, None, is_dry_run(), False, quote.error)
        if is_dry_run():
            logger.info("[DRY_RUN] CCTP USDC %s→%s: %.4f USDC", from_chain, to_chain, amount_usdc)
            return CctpBridgeResult(
                direction, amount_usdc, f"dry-run-cctp-{from_chain}", f"dry-run-cctp-{to_chain}", True, True
            )

        chains = load_chains()
        src = self._evm_executor(from_chain, chains)
        dst = self._evm_executor(to_chain, chains)
        amount_raw = from_human(amount_usdc, 6)
        max_fee = self._max_fee_raw(quote.fee_usd)

        source_tx = src.deposit_for_burn(
            token_messenger=self.cfg[f"{from_chain}_token_messenger"],
            usdc=self.cfg[f"{from_chain}_usdc"],
            amount=amount_raw,
            destination_domain=int(self.cfg[f"{to_chain}_domain"]),
            mint_recipient=_evm_addr_bytes32(dst.address),
            max_fee=max_fee,
            min_finality_threshold=int(self.cfg.get("fast_finality_threshold", FAST_FINALITY_THRESHOLD)),
        )
        if not source_tx:
            return CctpBridgeResult(
                direction, amount_usdc, None, None, False, False, f"{from_chain} depositForBurn failed"
            )
        log_tx(f"cctp_burn_{from_chain}_to_{to_chain}", from_chain, source_tx, extra={"amount_usdc": amount_usdc})

        att = await self._poll_or_queue(
            client, int(self.cfg[f"{from_chain}_domain"]), int(self.cfg[f"{to_chain}_domain"]), source_tx, direction
        )
        if att is None:
            return CctpBridgeResult(direction, amount_usdc, source_tx, None, False, False, "attestation timeout")

        dest_tx = dst.receive_message(
            message_transmitter=self.cfg[f"{to_chain}_message_transmitter"],
            message_hex=att.message,
            attestation_hex=att.attestation,
        )
        if not dest_tx:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg[f"{from_chain}_domain"]),
                dest_domain=int(self.cfg[f"{to_chain}_domain"]),
                intent=f"{direction}_claim_pending",
            )
            return CctpBridgeResult(
                direction, amount_usdc, source_tx, None, False, False, f"{to_chain} receiveMessage failed (queued)"
            )
        log_tx(f"cctp_claim_{to_chain}", to_chain, dest_tx, extra={"source_tx": source_tx})
        return CctpBridgeResult(direction, amount_usdc, source_tx, dest_tx, False, True)

    async def bridge_usdc_base_to_eth(self, client: httpx.AsyncClient, amount_usdc: float) -> CctpBridgeResult:
        return await self._bridge_usdc_evm_to_evm(client, "base", "ethereum", amount_usdc)

    async def bridge_usdc_eth_to_base(self, client: httpx.AsyncClient, amount_usdc: float) -> CctpBridgeResult:
        return await self._bridge_usdc_evm_to_evm(client, "ethereum", "base", amount_usdc)

    async def bridge_usdc_base_to_sol(
        self, client: httpx.AsyncClient, amount_usdc: float
    ) -> CctpBridgeResult:
        direction = "base_to_solana_usdc"
        quote = await self.quote_usdc(client, "base", "solana", amount_usdc)
        if not quote.ok:
            return CctpBridgeResult(direction, amount_usdc, None, None, is_dry_run(), False, quote.error)
        if is_dry_run():
            logger.info("[DRY_RUN] CCTP USDC Base→Sol: %.4f USDC", amount_usdc)
            return CctpBridgeResult(direction, amount_usdc, "dry-run-cctp-base", "dry-run-cctp-sol", True, True)

        import base58

        chains = load_chains()
        from src.execution.base import BaseExecutor

        base_exec = BaseExecutor(chains["base"])
        sol_exec = SolanaExecutor(chains["solana"])
        amount_raw = from_human(amount_usdc, 6)
        max_fee = self._max_fee_raw(quote.fee_usd)

        sol_ata = sol_usdc_ata(sol_exec.pubkey, self.cfg["solana_usdc"])
        mint_recipient = base58.b58decode(sol_ata)

        source_tx = base_exec.deposit_for_burn(
            token_messenger=self.cfg["base_token_messenger"],
            usdc=self.cfg["base_usdc"],
            amount=amount_raw,
            destination_domain=int(self.cfg["solana_domain"]),
            mint_recipient=mint_recipient,
            max_fee=max_fee,
            min_finality_threshold=int(self.cfg.get("fast_finality_threshold", FAST_FINALITY_THRESHOLD)),
        )
        if not source_tx:
            return CctpBridgeResult(direction, amount_usdc, None, None, False, False, "Base depositForBurn failed")
        log_tx("cctp_burn_base_to_sol", "base", source_tx, extra={"amount_usdc": amount_usdc})

        att = await self._poll_or_queue(
            client, int(self.cfg["base_domain"]), int(self.cfg["solana_domain"]), source_tx, direction
        )
        if att is None:
            return CctpBridgeResult(direction, amount_usdc, source_tx, None, False, False, "attestation timeout")

        dest_tx, err = run_receive_sol(
            message_hex=att.message,
            attestation_hex=att.attestation,
            sol_rpc=chains["solana"].rpc_url,
            sol_secret=os.getenv("SOLANA_SECRET_KEY", ""),
            sol_owner=sol_exec.pubkey,
            sol_usdc_mint=self.cfg["solana_usdc"],
            eth_domain=int(self.cfg["base_domain"]),
            eth_usdc=self.cfg["base_usdc"],
            iris_api=self.base,
        )
        if not dest_tx:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["base_domain"]),
                dest_domain=int(self.cfg["solana_domain"]),
                intent="base_to_sol_claim_pending",
            )
            return CctpBridgeResult(
                direction, amount_usdc, source_tx, None, False, False, err or "Sol receive failed (queued)"
            )
        log_tx("cctp_claim_sol", "solana", dest_tx, extra={"source_tx": source_tx})
        return CctpBridgeResult(direction, amount_usdc, source_tx, dest_tx, False, True)

    async def bridge_usdc_sol_to_base(
        self, client: httpx.AsyncClient, amount_usdc: float
    ) -> CctpBridgeResult:
        direction = "solana_to_base_usdc"
        quote = await self.quote_usdc(client, "solana", "base", amount_usdc)
        if not quote.ok:
            return CctpBridgeResult(direction, amount_usdc, None, None, is_dry_run(), False, quote.error)
        if is_dry_run():
            logger.info("[DRY_RUN] CCTP USDC Sol→Base: %.4f USDC", amount_usdc)
            return CctpBridgeResult(direction, amount_usdc, "dry-run-cctp-sol", "dry-run-cctp-base", True, True)

        chains = load_chains()
        from src.execution.base import BaseExecutor

        base_exec = BaseExecutor(chains["base"])
        sol_exec = SolanaExecutor(chains["solana"])
        amount_raw = from_human(amount_usdc, 6)
        max_fee = self._max_fee_raw(quote.fee_usd)

        source_tx, err = run_burn_sol(
            amount_raw=amount_raw,
            max_fee_raw=max_fee,
            min_finality_threshold=int(self.cfg.get("fast_finality_threshold", FAST_FINALITY_THRESHOLD)),
            sol_rpc=chains["solana"].rpc_url,
            sol_secret=os.getenv("SOLANA_SECRET_KEY", ""),
            sol_owner=sol_exec.pubkey,
            sol_usdc_mint=self.cfg["solana_usdc"],
            eth_domain=int(self.cfg["base_domain"]),
            eth_address=base_exec.address,
            eth_usdc=self.cfg["base_usdc"],
            iris_api=self.base,
        )
        if not source_tx:
            return CctpBridgeResult(direction, amount_usdc, None, None, False, False, err)
        log_tx("cctp_burn_sol_to_base", "solana", source_tx, extra={"amount_usdc": amount_usdc})

        att = await self._poll_or_queue(
            client, int(self.cfg["solana_domain"]), int(self.cfg["base_domain"]), source_tx, direction
        )
        if att is None:
            return CctpBridgeResult(direction, amount_usdc, source_tx, None, False, False, "attestation timeout")

        dest_tx = base_exec.receive_message(
            message_transmitter=self.cfg["base_message_transmitter"],
            message_hex=att.message,
            attestation_hex=att.attestation,
        )
        if not dest_tx:
            CctpClaimQueue().enqueue(
                source_tx=source_tx,
                source_domain=int(self.cfg["solana_domain"]),
                dest_domain=int(self.cfg["base_domain"]),
                intent="sol_to_base_claim_pending",
            )
            return CctpBridgeResult(
                direction, amount_usdc, source_tx, None, False, False, "Base receiveMessage failed (queued)"
            )
        log_tx("cctp_claim_base", "base", dest_tx, extra={"source_tx": source_tx})
        return CctpBridgeResult(direction, amount_usdc, source_tx, dest_tx, False, True)
