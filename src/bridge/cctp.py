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


class CircleCctpBridge:
    """Circle CCTP v2 — native USDC Solana ↔ Ethereum for VNX platform settlement."""

    def __init__(self) -> None:
        self.cfg = load_bridge_config()["cctp"]
        self.base = self.cfg["iris_api"].rstrip("/")

    async def quote_usdc(
        self,
        client: httpx.AsyncClient,
        from_chain: str,
        to_chain: str,
        amount_usdc: float,
    ) -> CctpQuote:
        direction = f"{from_chain}_to_{to_chain}"
        if {from_chain, to_chain} != {"solana", "ethereum"}:
            return CctpQuote("cctp", direction, amount_usdc, 0, 0, "unsupported pair")

        src_dom = self.cfg["solana_domain"] if from_chain == "solana" else self.cfg["ethereum_domain"]
        dst_dom = self.cfg["ethereum_domain"] if to_chain == "ethereum" else self.cfg["solana_domain"]

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
