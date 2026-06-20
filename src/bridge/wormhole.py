from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from web3 import Web3

from src.config_loader import ChainConfig, is_dry_run, load_bridge_config, load_chains
from src.execution.base import BaseExecutor, ERC20_ABI
from src.execution.ethereum import EthereumExecutor
from src.quotes.addresses import checksum

logger = logging.getLogger(__name__)

# Wormhole Token Bridge — transferTokens (simplified ABI)
TOKEN_BRIDGE_ABI = [
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "recipientChain", "type": "uint16"},
            {"name": "recipient", "type": "bytes32"},
            {"name": "arbiterFee", "type": "uint256"},
            {"name": "nonce", "type": "uint32"},
        ],
        "name": "transferTokens",
        "outputs": [{"name": "sequence", "type": "uint64"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "impl", "type": "address"}],
        "name": "registerToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _solana_address_to_bytes32(base58_addr: str) -> bytes:
    import base58

    raw = base58.b58decode(base58_addr)
    if len(raw) != 32:
        raise ValueError(f"Solana pubkey must be 32 bytes, got {len(raw)}")
    return raw


def _evm_address_to_bytes32(evm_addr: str) -> bytes:
    addr = checksum(evm_addr)
    return bytes.fromhex(addr[2:].rjust(64, "0"))


@dataclass
class WormholeQuote:
    provider: str
    amount_in_usdt: float
    amount_out_usdt: float
    fee_usd: float
    from_chain: str
    to_chain: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.amount_out_usdt > 0


@dataclass
class WormholeBridgeResult:
    direction: str
    amount_usdt: float
    source_tx: str | None
    dest_tx: str | None
    dry_run: bool
    success: bool
    error: str | None = None


class WormholePortalBridge:
    """
    Wormhole Portal Token Bridge for native USDT between Base and Solana.

    Security: 19 Guardian validators, 13/19 quorum (industry-standard; listed on Base docs).
    """

    def __init__(self, base_chain: ChainConfig) -> None:
        self.base_chain = base_chain
        self.cfg = load_bridge_config()
        self.wh = self.cfg["wormhole"]

    _SUPPORTED = frozenset({"base", "solana", "ethereum"})

    @staticmethod
    def _initiate_receipt_ok(w3, tx_hash: str) -> bool:
        from src.bridge.wormhole_vaa import parse_sequence_from_receipt

        tx_hash = tx_hash if str(tx_hash).startswith("0x") else f"0x{tx_hash}"
        try:
            rcpt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return False
        if rcpt.status != 1 or int(rcpt.gasUsed) < 80_000:
            return False
        logs = [
            {
                "address": log.address,
                "topics": [t.hex() if hasattr(t, "hex") else t for t in log.topics],
                "data": log.data.hex() if hasattr(log.data, "hex") else log.data,
            }
            for log in rcpt.logs
        ]
        return parse_sequence_from_receipt({"logs": logs}) is not None

    def quote_usdt(self, from_chain: str, to_chain: str, amount_usdt: float) -> WormholeQuote:
        if from_chain not in self._SUPPORTED or to_chain not in self._SUPPORTED:
            return WormholeQuote(
                "wormhole", amount_usdt, 0, 0, from_chain, to_chain, error="unsupported chain pair"
            )
        if from_chain == to_chain:
            return WormholeQuote(
                "wormhole", amount_usdt, amount_usdt, 0, from_chain, to_chain, error="same chain"
            )
        evm_pairs = {("base", "ethereum"), ("ethereum", "base"), ("base", "solana")}
        if (from_chain, to_chain) not in evm_pairs and from_chain != "base":
            return WormholeQuote(
                "wormhole",
                amount_usdt,
                0,
                0,
                from_chain,
                to_chain,
                error=f"USDT initiate from {from_chain} not supported (use base or ethereum EVM)",
            )
        fee = float(self.wh.get("fee_usd_estimate", 0.5))
        out = max(0.0, amount_usdt - fee)
        return WormholeQuote("wormhole", amount_usdt, out, fee, from_chain, to_chain)

    def simulate_base_transfer_tokens(
        self,
        amount_usdt: float,
        dest_chain: str,
        recipient: str,
        base_exec: BaseExecutor | None = None,
    ) -> dict:
        """eth_call + gas estimate for transferTokens on Base (no broadcast)."""
        if dest_chain not in ("solana", "ethereum"):
            return {"ok": False, "error": f"unsupported dest {dest_chain}"}
        amount_raw = int(amount_usdt * 10**6)
        token = checksum(self.wh["base_usdc"])
        bridge = checksum(self.wh["base_token_bridge"])
        if dest_chain == "solana":
            recipient_b32 = _solana_address_to_bytes32(recipient)
            dest_chain_id = int(self.wh["solana_chain_id"])
        else:
            recipient_b32 = _evm_address_to_bytes32(recipient)
            dest_chain_id = int(self.wh["ethereum_chain_id"])

        exec_ = base_exec or BaseExecutor(self.base_chain)
        w3 = exec_.w3
        contract = w3.eth.contract(address=bridge, abi=TOKEN_BRIDGE_ABI)
        nonce = int(time.time()) & 0xFFFFFFFF
        fn = contract.functions.transferTokens(
            checksum(token),
            amount_raw,
            dest_chain_id,
            recipient_b32,
            0,
            nonce,
        )
        balance = exec_.balance_erc20(token)
        allowance = w3.eth.contract(address=token, abi=ERC20_ABI).functions.allowance(
            exec_.address, bridge
        ).call()
        out: dict = {
            "dest_chain": dest_chain,
            "dest_chain_id": dest_chain_id,
            "amount_usdt": amount_usdt,
            "balance_usdt": balance / 10**6,
            "allowance_usdt": allowance / 10**6,
            "bridge": bridge,
            "token": token,
        }
        if balance < amount_raw:
            out["ok"] = False
            out["error"] = "insufficient USDT on Base hot wallet"
            return out
        try:
            fn.call({"from": exec_.address, "value": 0})
            gas = fn.estimate_gas({"from": exec_.address, "value": 0})
            out["ok"] = True
            out["gas_estimate"] = gas
            out["needs_approval"] = allowance < amount_raw
        except Exception as exc:
            err = str(exc)
            if allowance < amount_raw and "allowance" in err.lower() and balance >= amount_raw:
                out["ok"] = True
                out["needs_approval"] = True
                out["note"] = "transferTokens reverts until bridge approved; balance sufficient"
                try:
                    approve_fn = w3.eth.contract(address=token, abi=ERC20_ABI).functions.approve(
                        bridge, amount_raw
                    )
                    out["approve_gas_estimate"] = approve_fn.estimate_gas({"from": exec_.address})
                except Exception as approve_exc:
                    out["approve_gas_error"] = str(approve_exc)
            else:
                out["ok"] = False
                out["error"] = err
        return out

    def simulate_eth_transfer_tokens(
        self,
        amount_usdt: float,
        base_recipient: str,
        eth_exec: EthereumExecutor | None = None,
    ) -> dict:
        """eth_call + gas estimate for ETH USDT → Base via Wormhole (no broadcast)."""
        amount_raw = int(amount_usdt * 10**6)
        token = checksum(self.wh["ethereum_usdt"])
        bridge = checksum(self.wh["ethereum_token_bridge"])
        recipient_b32 = _evm_address_to_bytes32(base_recipient)
        dest_chain_id = int(self.wh["base_chain_id"])

        exec_ = eth_exec or EthereumExecutor(load_chains()["ethereum"])
        w3 = exec_.w3
        contract = w3.eth.contract(address=bridge, abi=TOKEN_BRIDGE_ABI)
        nonce = int(time.time()) & 0xFFFFFFFF
        fn = contract.functions.transferTokens(
            token,
            amount_raw,
            dest_chain_id,
            recipient_b32,
            0,
            nonce,
        )
        balance = exec_.balance_erc20(token)
        allowance = w3.eth.contract(address=token, abi=ERC20_ABI).functions.allowance(
            exec_.address, bridge
        ).call()
        out: dict = {
            "dest_chain": "base",
            "dest_chain_id": dest_chain_id,
            "amount_usdt": amount_usdt,
            "balance_usdt": balance / 10**6,
            "allowance_usdt": allowance / 10**6,
            "bridge": bridge,
            "token": token,
        }
        if balance < amount_raw:
            out["ok"] = False
            out["error"] = "insufficient USDT on ETH hot wallet"
            return out
        try:
            fn.call({"from": exec_.address, "value": 0})
            gas = fn.estimate_gas({"from": exec_.address, "value": 0})
            out["ok"] = True
            out["gas_estimate"] = gas
            if allowance < amount_raw:
                out["needs_approval"] = True
                out["note"] = "approve Token Bridge before initiate"
        except Exception as exc:
            err = str(exc)
            if allowance < amount_raw and "allowance" in err.lower():
                out["ok"] = True
                out["needs_approval"] = True
                out["note"] = "approve bridge first"
            else:
                out["ok"] = False
                out["error"] = err
        return out

    async def bridge_usdt_base_to_solana(
        self,
        amount_usdt: float,
        solana_recipient: str,
        base_exec: BaseExecutor | None = None,
    ) -> WormholeBridgeResult:
        direction = "base_to_solana_usdt"
        quote = self.quote_usdt("base", "solana", amount_usdt)
        if not quote.ok:
            return WormholeBridgeResult(direction, amount_usdt, None, None, is_dry_run(), False, quote.error)

        amount_raw = int(amount_usdt * 10**6)
        token = checksum(self.wh["base_usdc"])
        bridge = checksum(self.wh["base_token_bridge"])
        recipient = _solana_address_to_bytes32(solana_recipient)
        dest_chain_id = int(self.wh["solana_chain_id"])

        if is_dry_run():
            logger.info(
                "[DRY_RUN] Wormhole USDT %s → Solana: %.4f USDT to %s",
                direction,
                amount_usdt,
                solana_recipient[:8],
            )
            return WormholeBridgeResult(direction, amount_usdt, "dry-run-wormhole-base", None, True, True)

        exec_ = base_exec or BaseExecutor(self.base_chain)
        exec_.approve_if_needed(token, bridge, amount_raw)

        w3 = exec_.w3
        contract = w3.eth.contract(address=bridge, abi=TOKEN_BRIDGE_ABI)
        nonce = int(time.time()) & 0xFFFFFFFF
        tx = contract.functions.transferTokens(
            checksum(token),
            amount_raw,
            dest_chain_id,
            recipient,
            0,
            nonce,
        ).build_transaction(exec_._tx_base())

        tx_hash = exec_._build_and_send(tx)
        if not tx_hash:
            return WormholeBridgeResult(direction, amount_usdt, None, None, False, False, "base transferTokens failed")
        if not self._initiate_receipt_ok(w3, tx_hash):
            return WormholeBridgeResult(
                direction,
                amount_usdt,
                tx_hash,
                None,
                False,
                False,
                "Base Wormhole initiate missing LogMessagePublished",
            )

        logger.info("Wormhole initiated on Base: %s (redeem on Solana via VAA)", tx_hash)
        return WormholeBridgeResult(direction, amount_usdt, tx_hash, None, False, True)

    async def bridge_usdt_base_to_ethereum(
        self,
        amount_usdt: float,
        ethereum_recipient: str,
        base_exec: BaseExecutor | None = None,
    ) -> WormholeBridgeResult:
        direction = "base_to_ethereum_usdt"
        quote = self.quote_usdt("base", "ethereum", amount_usdt)
        if not quote.ok:
            return WormholeBridgeResult(direction, amount_usdt, None, None, is_dry_run(), False, quote.error)

        amount_raw = int(amount_usdt * 10**6)
        token = checksum(self.wh["base_usdc"])
        bridge = checksum(self.wh["base_token_bridge"])
        recipient = _evm_address_to_bytes32(ethereum_recipient)
        dest_chain_id = int(self.wh["ethereum_chain_id"])

        if is_dry_run():
            logger.info(
                "[DRY_RUN] Wormhole USDT %s → Ethereum: %.4f USDT to %s",
                direction,
                amount_usdt,
                ethereum_recipient[:10],
            )
            return WormholeBridgeResult(direction, amount_usdt, "dry-run-wormhole-base-eth", None, True, True)

        exec_ = base_exec or BaseExecutor(self.base_chain)
        exec_.approve_if_needed(token, bridge, amount_raw)

        w3 = exec_.w3
        contract = w3.eth.contract(address=bridge, abi=TOKEN_BRIDGE_ABI)
        nonce = int(time.time()) & 0xFFFFFFFF
        tx = contract.functions.transferTokens(
            checksum(token),
            amount_raw,
            dest_chain_id,
            recipient,
            0,
            nonce,
        ).build_transaction(exec_._tx_base())

        tx_hash = exec_._build_and_send(tx)
        if not tx_hash:
            return WormholeBridgeResult(
                direction, amount_usdt, None, None, False, False, "base transferTokens to ETH failed"
            )

        logger.info("Wormhole initiated on Base: %s (redeem on Ethereum via VAA)", tx_hash)
        return WormholeBridgeResult(direction, amount_usdt, tx_hash, None, False, True)

    async def bridge_usdt_solana_to_base(
        self,
        amount_usdt: float,
        base_recipient: str,
    ) -> WormholeBridgeResult:
        """Solana→Base USDT via Wormhole (dry-run logs; live requires SPL + redeem flow)."""
        direction = "solana_to_base_usdc"
        quote = self.quote_usdt("solana", "base", amount_usdt)
        if not quote.ok:
            return WormholeBridgeResult(direction, amount_usdt, None, None, is_dry_run(), False, quote.error)

        if is_dry_run():
            logger.info(
                "[DRY_RUN] Wormhole USDT Solana → Base: %.4f USDT to %s",
                amount_usdt,
                base_recipient[:10],
            )
            return WormholeBridgeResult(direction, amount_usdt, "dry-run-wormhole-sol", None, True, True)

        # Live Solana initiate requires @wormhole-foundation/sdk SPL transfer — phase 2
        return WormholeBridgeResult(
            direction,
            amount_usdt,
            None,
            None,
            False,
            False,
            "Solana Wormhole initiate: use scripts/wormhole_bridge.py or add TS helper",
        )

    async def bridge_usdt_ethereum_to_base(
        self,
        amount_usdt: float,
        base_recipient: str,
        eth_exec: EthereumExecutor | None = None,
    ) -> WormholeBridgeResult:
        """Ethereum USDT → Base USDT via Wormhole Portal (initiate only; redeem via queue)."""
        direction = "ethereum_to_base_usdc"
        quote = self.quote_usdt("ethereum", "base", amount_usdt)
        if not quote.ok:
            return WormholeBridgeResult(direction, amount_usdt, None, None, is_dry_run(), False, quote.error)

        amount_raw = int(amount_usdt * 10**6)
        token = checksum(self.wh["ethereum_usdt"])
        bridge = checksum(self.wh["ethereum_token_bridge"])
        recipient = _evm_address_to_bytes32(base_recipient)
        dest_chain_id = int(self.wh["base_chain_id"])

        if is_dry_run():
            logger.info(
                "[DRY_RUN] Wormhole USDT %s → Base: %.4f USDT to %s",
                direction,
                amount_usdt,
                base_recipient[:10],
            )
            return WormholeBridgeResult(direction, amount_usdt, "dry-run-wormhole-eth-base", None, True, True)

        exec_ = eth_exec or EthereumExecutor(load_chains()["ethereum"])
        tx_hash = exec_.transfer_tokens_wormhole(
            bridge=bridge,
            token=token,
            amount=amount_raw,
            dest_chain_id=dest_chain_id,
            recipient=recipient,
        )
        if not tx_hash or tx_hash == "already-claimed":
            return WormholeBridgeResult(
                direction, amount_usdt, None, None, False, False, exec_.last_error or "ETH transferTokens failed"
            )
        if not self._initiate_receipt_ok(exec_.w3, tx_hash):
            return WormholeBridgeResult(
                direction,
                amount_usdt,
                tx_hash,
                None,
                False,
                False,
                "ETH Wormhole initiate tx has no LogMessagePublished (likely insufficient USDT/gas)",
            )
        logger.info("Wormhole initiated on ETH: %s (redeem on Base via VAA)", tx_hash)
        return WormholeBridgeResult(direction, amount_usdt, tx_hash, None, False, True)

    async def bridge_usdt_with_redeem(
        self,
        client,
        *,
        from_chain: str,
        to_chain: str,
        amount_usdt: float,
        recipient: str,
        intent: str = "wormhole_usdt",
    ) -> WormholeBridgeResult:
        """Initiate Wormhole USDT transfer and claim on destination when VAA is ready."""
        from src.bridge.wormhole_queue import WormholeClaimQueue
        from src.config_loader import load_chains

        if from_chain == "base" and to_chain == "ethereum":
            br = await self.bridge_usdt_base_to_ethereum(amount_usdt, recipient)
        elif from_chain == "base" and to_chain == "solana":
            br = await self.bridge_usdt_base_to_solana(amount_usdt, recipient)
        elif from_chain == "ethereum" and to_chain == "base":
            br = await self.bridge_usdt_ethereum_to_base(amount_usdt, recipient)
        else:
            return WormholeBridgeResult(
                f"{from_chain}_to_{to_chain}_usdt",
                amount_usdt,
                None,
                None,
                is_dry_run(),
                False,
                f"unsupported wormhole pair {from_chain}→{to_chain}",
            )
        if not br.success or not br.source_tx:
            return br

        wh = self.wh
        source_chain_id = int(wh[f"{from_chain}_chain_id"])
        emitter = WormholeClaimQueue.emitter_for_chain(from_chain)
        queue = WormholeClaimQueue()
        queue.enqueue(
            source_chain=from_chain,
            dest_chain=to_chain,
            source_tx=br.source_tx,
            source_chain_id=source_chain_id,
            emitter=emitter,
            intent=intent,
        )
        summary = await queue.run_until_empty(client)
        item = next((i for i in queue._store.items if i.source_tx.lower() == br.source_tx.lower()), None)
        if item and item.dest_tx:
            br = WormholeBridgeResult(
                br.direction, br.amount_usdt, br.source_tx, item.dest_tx, br.dry_run, True, None
            )
        elif summary["remaining"] > 0:
            br = WormholeBridgeResult(
                br.direction,
                br.amount_usdt,
                br.source_tx,
                None,
                br.dry_run,
                False,
                "VAA claim pending — run wormhole claim worker",
            )
        return br
