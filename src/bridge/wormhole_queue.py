from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from src.bridge.wormhole_vaa import fetch_signed_vaa, fetch_vaa_by_tx_hash, token_bridge_emitter
from src.config_loader import data_dir, is_dry_run, load_bridge_config, load_chains
from src.execution.base import BaseExecutor
from src.execution.ethereum import EthereumExecutor
from src.execution.tx_log import log_tx

logger = logging.getLogger(__name__)

def wormhole_queue_path() -> Path:
    return data_dir() / "wormhole_queue.json"


class WormholeQueueStatus(str, Enum):
    PENDING_VAA = "pending_vaa"
    READY = "ready_to_claim"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    FAILED = "failed"


@dataclass
class WormholePendingItem:
    source_chain: str
    dest_chain: str
    source_tx: str
    source_chain_id: int
    emitter: str
    sequence: int | None = None
    intent: str = "wormhole_usdt"
    status: str = WormholeQueueStatus.PENDING_VAA.value
    dest_tx: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.source_chain}:{self.source_tx.lower()}"
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @property
    def terminal(self) -> bool:
        return self.status in (WormholeQueueStatus.CLAIMED.value, WormholeQueueStatus.FAILED.value)


@dataclass
class WormholeQueueStore:
    items: list[WormholePendingItem] = field(default_factory=list)
    version: int = 1


class WormholeClaimQueue:
    """Track Wormhole Portal initiates awaiting VAA + completeTransfer on destination."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or wormhole_queue_path()
        self.cfg = load_bridge_config()["wormhole"]
        self._store = self.load()

    def load(self) -> WormholeQueueStore:
        if not self.path.exists():
            return WormholeQueueStore()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = [WormholePendingItem(**row) for row in raw.get("items") or []]
            return WormholeQueueStore(items=items, version=int(raw.get("version") or 1))
        except Exception as exc:
            logger.warning("Wormhole queue load failed (%s), starting fresh", exc)
            return WormholeQueueStore()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._store.version,
            "items": [asdict(i) for i in self._store.items],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def enqueue(
        self,
        *,
        source_chain: str,
        dest_chain: str,
        source_tx: str,
        source_chain_id: int,
        emitter: str,
        sequence: int | None = None,
        intent: str = "wormhole_usdt",
    ) -> WormholePendingItem:
        tx = source_tx.lower()
        for item in self._store.items:
            if item.source_tx.lower() == tx and item.source_chain == source_chain:
                if sequence and not item.sequence:
                    item.sequence = sequence
                self.save()
                return item
        item = WormholePendingItem(
            source_chain=source_chain,
            dest_chain=dest_chain,
            source_tx=tx,
            source_chain_id=source_chain_id,
            emitter=emitter,
            sequence=sequence,
            intent=intent,
        )
        self._store.items.append(item)
        self.save()
        logger.info("Wormhole queued %s→%s tx=%s", source_chain, dest_chain, tx[:18])
        try:
            from src.treasury.in_flight import InFlightLedger

            InFlightLedger(os.getenv("TOKEN_ASSET", "VCHF")).log_wormhole_burn(
                tx,
                source_chain,
                dest_chain,
                intent=intent,
            )
        except Exception as exc:
            logger.debug("Wormhole in-flight log skip: %s", exc)
        return item

    async def run_until_empty(
        self,
        client: httpx.AsyncClient,
        *,
        interval_sec: float | None = None,
        max_rounds: int = 120,
    ) -> dict[str, Any]:
        interval = interval_sec or float(self.cfg.get("claim_interval_sec", 30))
        claimed = 0
        rounds = 0
        for _ in range(max_rounds):
            pending = [i for i in self._store.items if not i.terminal]
            if not pending:
                break
            rounds += 1
            for item in pending:
                ok = await self._process_item(client, item)
                if ok and item.status == WormholeQueueStatus.CLAIMED.value:
                    claimed += 1
            self.save()
            if all(i.terminal for i in self._store.items if i in pending):
                break
            await asyncio.sleep(interval)
        remaining = sum(1 for i in self._store.items if not i.terminal)
        return {"claimed": claimed, "remaining": remaining, "rounds": rounds}

    async def _process_item(self, client: httpx.AsyncClient, item: WormholePendingItem) -> bool:
        if item.terminal:
            return True
        if item.status == WormholeQueueStatus.CLAIMING.value:
            item.status = WormholeQueueStatus.READY.value
        try:
            if item.sequence is None:
                item.sequence = self._sequence_from_receipt(item)
                if item.sequence is None and not self._initiate_receipt_valid(item):
                    item.status = WormholeQueueStatus.FAILED.value
                    item.error = item.error or "initiate tx missing LogMessagePublished"
                    return False
            vaa = await self._resolve_vaa(client, item)
            if not vaa:
                if item.sequence is None and not self._initiate_receipt_valid(item):
                    item.status = WormholeQueueStatus.FAILED.value
                    item.error = item.error or "no VAA and invalid initiate receipt"
                return False
            item.status = WormholeQueueStatus.READY.value
            item.updated_at = datetime.now(timezone.utc).isoformat()
            if is_dry_run():
                item.dest_tx = "dry-run-wormhole-claim"
                item.status = WormholeQueueStatus.CLAIMED.value
                return True
            item.status = WormholeQueueStatus.CLAIMING.value
            dest_tx = await self._claim_on_dest(item, vaa)
            if dest_tx:
                item.dest_tx = dest_tx if dest_tx != "already-claimed" else item.dest_tx or dest_tx
                item.status = WormholeQueueStatus.CLAIMED.value
                if dest_tx != "already-claimed":
                    log_tx(item.intent, item.dest_chain, dest_tx)
                if item.dest_chain == "base" and "eth" in item.source_chain:
                    from src.bridge.base_usdc import consolidate_after_eth_to_base_redeem

                    con = await consolidate_after_eth_to_base_redeem()
                    if con.get("success") and not con.get("skipped"):
                        logger.info("Auto-consolidated wrapped USDT → canonical on Base")
                return True
            item.status = WormholeQueueStatus.READY.value
            return False
        except Exception as exc:
            item.error = str(exc)[:300]
            item.status = WormholeQueueStatus.FAILED.value
            logger.error("Wormhole claim failed %s: %s", item.source_tx[:18], exc)
            return False
        finally:
            item.updated_at = datetime.now(timezone.utc).isoformat()

    async def _resolve_vaa(self, client: httpx.AsyncClient, item: WormholePendingItem) -> bytes | None:
        if item.sequence is None:
            item.sequence = self._sequence_from_receipt(item)
            if item.sequence is not None:
                self.save()
        if item.sequence is not None:
            vaa = await fetch_signed_vaa(
                client,
                chain_id=item.source_chain_id,
                emitter=item.emitter,
                sequence=item.sequence,
                timeout_sec=float(self.cfg.get("attestation_poll_sec", 15)) * 4,
            )
            if vaa:
                return vaa
        return await fetch_vaa_by_tx_hash(
            client,
            item.source_tx,
            timeout_sec=float(self.cfg.get("attestation_poll_sec", 15)) * 4,
        )

    def _initiate_receipt_valid(self, item: WormholePendingItem) -> bool:
        from src.bridge.wormhole import WormholePortalBridge

        chains = load_chains()
        tx = item.source_tx if item.source_tx.startswith("0x") else f"0x{item.source_tx}"
        try:
            if item.source_chain == "base":
                exec_ = BaseExecutor(chains["base"])
                return WormholePortalBridge._initiate_receipt_ok(exec_.w3, tx)
            if item.source_chain == "ethereum":
                eth = EthereumExecutor(chains["ethereum"])
                return WormholePortalBridge._initiate_receipt_ok(eth.w3, tx)
            if item.source_chain == "celo":
                from src.execution.celo import CeloExecutor

                celo = CeloExecutor(chains["celo"])
                return WormholePortalBridge._initiate_receipt_ok(celo.w3, tx)
        except Exception:
            pass
        return False

    def _sequence_from_receipt(self, item: WormholePendingItem) -> int | None:
        """Read LogMessagePublished sequence from source-chain initiate tx."""
        from src.bridge.wormhole_vaa import parse_sequence_from_receipt

        chains = load_chains()
        tx = item.source_tx if item.source_tx.startswith("0x") else f"0x{item.source_tx}"
        try:
            if item.source_chain == "base":
                from src.execution.base import BaseExecutor

                exec_ = BaseExecutor(chains["base"])
                rcpt = exec_.w3.eth.get_transaction_receipt(tx)
            elif item.source_chain == "ethereum":
                eth = EthereumExecutor(chains["ethereum"])
                rcpt = eth.w3.eth.get_transaction_receipt(tx)
            elif item.source_chain == "celo":
                from src.execution.celo import CeloExecutor

                celo = CeloExecutor(chains["celo"])
                rcpt = celo.w3.eth.get_transaction_receipt(tx)
            else:
                return None
            logs = []
            for log in rcpt.logs:
                logs.append(
                    {
                        "address": log.address,
                        "topics": [t.hex() if hasattr(t, "hex") else t for t in log.topics],
                        "data": log.data.hex() if hasattr(log.data, "hex") else log.data,
                    }
                )
            seq = parse_sequence_from_receipt({"logs": logs})
            if seq is not None:
                logger.info("Wormhole sequence %s for %s", seq, tx[:18])
            return seq
        except Exception as exc:
            logger.debug("sequence from receipt %s: %s", tx[:18], exc)
            return None

    async def _claim_on_dest(self, item: WormholePendingItem, vaa: bytes) -> str | None:
        chains = load_chains()
        if item.dest_chain == "ethereum":
            eth = EthereumExecutor(chains["ethereum"])
            bridge = self.cfg.get("ethereum_token_bridge") or ""
            return eth.complete_transfer_wormhole(bridge, vaa)
        if item.dest_chain == "base":
            base = BaseExecutor(chains["base"])
            bridge = self.cfg["base_token_bridge"]
            return base.complete_transfer_wormhole(bridge, vaa)
        raise ValueError(f"unsupported wormhole dest {item.dest_chain}")

    @staticmethod
    def emitter_for_chain(chain: str) -> str:
        wh = load_bridge_config()["wormhole"]
        if chain == "base":
            return token_bridge_emitter(wh["base_token_bridge"])
        if chain == "ethereum":
            return token_bridge_emitter(wh["ethereum_token_bridge"])
        if chain == "celo":
            return token_bridge_emitter(wh["celo_token_bridge"])
        raise ValueError(f"no wormhole emitter for {chain}")
