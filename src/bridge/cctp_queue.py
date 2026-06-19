from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from src.bridge.cctp_iris import CctpAttestation, fetch_messages, normalize_cctp_tx_hash
from src.bridge.cctp_sol import run_receive_sol, sol_usdc_ata
from src.config_loader import ROOT, is_dry_run, load_bridge_config, load_chains
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.execution.tx_log import log_tx
from src.quotes.rpc_json import post_json_rpc_sync

logger = logging.getLogger(__name__)

QUEUE_PATH = ROOT / "data" / "cctp_queue.json"

SOL_CCTP_PROGRAM = "CCTPV2vPZJS2u2BBsUoscuikbYjnpFmbFsvVuJdgUMQe"
ETH_TOKEN_MESSENGER = "0x28b5a0e9c621a5badaa536219b3a228c8168cf5d"


class CctpQueueStatus(str, Enum):
    PENDING_ATTESTATION = "pending_attestation"
    READY = "ready_to_claim"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    FAILED = "failed"


@dataclass
class CctpPendingItem:
    source_domain: int
    source_tx: str
    dest_domain: int
    intent: str = "cctp_bridge"
    status: str = CctpQueueStatus.PENDING_ATTESTATION.value
    dest_tx: str | None = None
    error: str | None = None
    message_hex: str | None = None
    attestation_hex: str | None = None
    attestation_polls: int = 0
    created_at: str = ""
    updated_at: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        eth_dom = int(load_bridge_config()["cctp"]["ethereum_domain"])
        self.source_tx = normalize_cctp_tx_hash(self.source_domain, self.source_tx, ethereum_domain=eth_dom)
        if not self.id:
            self.id = f"{self.source_domain}:{self.source_tx}"
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @property
    def dest_chain(self) -> str:
        cfg = load_bridge_config()["cctp"]
        if self.dest_domain == int(cfg["ethereum_domain"]):
            return "ethereum"
        if self.dest_domain == int(cfg["solana_domain"]):
            return "solana"
        return "unknown"

    @property
    def terminal(self) -> bool:
        return self.status in (CctpQueueStatus.CLAIMED.value, CctpQueueStatus.FAILED.value)


@dataclass
class CctpQueueStore:
    items: list[CctpPendingItem] = field(default_factory=list)
    version: int = 1

    def by_id(self) -> dict[str, CctpPendingItem]:
        return {i.id: i for i in self.items}

    def pending(self) -> list[CctpPendingItem]:
        return [i for i in self.items if not i.terminal]


class CctpClaimQueue:
    """Track CCTP burns awaiting attestation/claim; poll Iris and receive on dest chain."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or QUEUE_PATH
        self.cfg = load_bridge_config()["cctp"]
        self._store = self.load()

    def load(self) -> CctpQueueStore:
        if not self.path.exists():
            return CctpQueueStore()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = [CctpPendingItem(**row) for row in raw.get("items") or []]
            store = CctpQueueStore(items=items, version=int(raw.get("version") or 1))
            self._recover_stuck(store)
            CctpClaimQueue._coalesce_duplicates(store)
            return store
        except Exception as exc:
            logger.warning("CCTP queue load failed (%s), starting fresh", exc)
            return CctpQueueStore()

    @staticmethod
    def _recover_stuck(store: CctpQueueStore) -> None:
        """Reset items stuck in 'claiming' back to ready."""
        for item in store.items:
            if item.status == CctpQueueStatus.CLAIMING.value:
                item.status = CctpQueueStatus.READY.value
                item.updated_at = datetime.now(timezone.utc).isoformat()
                logger.info("CCTP recovered stuck item %s → ready", item.source_tx[:16])

    @staticmethod
    def _item_rank(item: CctpPendingItem) -> tuple[int, int]:
        """Higher = prefer keeping this duplicate."""
        status_rank = {
            CctpQueueStatus.CLAIMED.value: 4,
            CctpQueueStatus.READY.value: 3,
            CctpQueueStatus.PENDING_ATTESTATION.value: 2,
            CctpQueueStatus.CLAIMING.value: 1,
            CctpQueueStatus.FAILED.value: 0,
        }
        return (status_rank.get(item.status, 0), 1 if item.dest_tx else 0)

    @classmethod
    def _coalesce_duplicates(cls, store: CctpQueueStore) -> None:
        """Merge duplicate queue rows for the same burn (e.g. with/without 0x prefix)."""
        eth_dom = int(load_bridge_config()["cctp"]["ethereum_domain"])
        groups: dict[str, list[CctpPendingItem]] = {}
        for item in store.items:
            key = f"{item.source_domain}:{normalize_cctp_tx_hash(item.source_domain, item.source_tx, ethereum_domain=eth_dom)}"
            groups.setdefault(key, []).append(item)
        merged: list[CctpPendingItem] = []
        for key, items in groups.items():
            if len(items) == 1:
                items[0].source_tx = key.split(":", 1)[1]
                items[0].id = key
                merged.append(items[0])
                continue
            best = max(items, key=cls._item_rank)
            best.source_tx = key.split(":", 1)[1]
            best.id = key
            for other in items:
                if other is best:
                    continue
                if other.status == CctpQueueStatus.CLAIMED.value and best.status != CctpQueueStatus.CLAIMED.value:
                    best.status = CctpQueueStatus.CLAIMED.value
                    best.dest_tx = other.dest_tx or best.dest_tx
                    best.message_hex = best.message_hex or other.message_hex
                    best.attestation_hex = best.attestation_hex or other.attestation_hex
                    best.error = other.error or best.error
                elif best.status == CctpQueueStatus.CLAIMED.value and other.status != CctpQueueStatus.CLAIMED.value:
                    pass
                elif other.message_hex and not best.message_hex:
                    best.message_hex = other.message_hex
                    best.attestation_hex = other.attestation_hex
                logger.info(
                    "CCTP coalesced duplicate %s (dropped %s)",
                    best.source_tx[:18],
                    other.source_tx[:18],
                )
            merged.append(best)
        store.items = merged

    def _find_existing(self, source_domain: int, source_tx: str) -> CctpPendingItem | None:
        eth_dom = int(self.cfg["ethereum_domain"])
        norm = normalize_cctp_tx_hash(source_domain, source_tx, ethereum_domain=eth_dom)
        key = f"{source_domain}:{norm}"
        for item in self._store.items:
            if item.id == key:
                return item
        return None

    def _resolve_claimed_siblings(self) -> None:
        """If a duplicate burn was already claimed, close pending twins."""
        eth_dom = int(self.cfg["ethereum_domain"])
        claimed: dict[str, CctpPendingItem] = {}
        for item in self._store.items:
            if item.status == CctpQueueStatus.CLAIMED.value:
                key = f"{item.source_domain}:{normalize_cctp_tx_hash(item.source_domain, item.source_tx, ethereum_domain=eth_dom)}"
                claimed[key] = item
        changed = False
        for item in self._store.pending():
            key = f"{item.source_domain}:{normalize_cctp_tx_hash(item.source_domain, item.source_tx, ethereum_domain=eth_dom)}"
            sibling = claimed.get(key)
            if not sibling:
                continue
            item.status = CctpQueueStatus.CLAIMED.value
            item.dest_tx = sibling.dest_tx
            item.error = sibling.error or "already_claimed_duplicate"
            item.updated_at = datetime.now(timezone.utc).isoformat()
            changed = True
            logger.info("CCTP skip claim — already claimed (sibling) %s", item.source_tx[:18])
        if changed:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._store.version,
            "items": [asdict(i) for i in self._store.items],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def enqueue(
        self,
        *,
        source_tx: str,
        source_domain: int,
        dest_domain: int,
        intent: str = "cctp_bridge",
    ) -> tuple[CctpPendingItem, bool]:
        source_tx = source_tx.strip()
        eth_dom = int(self.cfg["ethereum_domain"])
        source_tx = normalize_cctp_tx_hash(source_domain, source_tx, ethereum_domain=eth_dom)
        existing = self._find_existing(source_domain, source_tx)
        if existing:
            return existing, False
        item = CctpPendingItem(
            source_domain=source_domain,
            source_tx=source_tx,
            dest_domain=dest_domain,
            intent=intent,
        )
        self._store.items.append(item)
        self.save()
        logger.info("CCTP queued %s → domain %s (%s)", source_tx, dest_domain, intent)
        return item, True

    async def discover(self, client: httpx.AsyncClient) -> int:
        """Scan wallet history for CCTP burns not yet in queue."""
        added = 0
        chains = load_chains()
        sol_exec = SolanaExecutor(chains["solana"])
        sol_domain = int(self.cfg["solana_domain"])
        eth_domain = int(self.cfg["ethereum_domain"])
        known = {
            normalize_cctp_tx_hash(i.source_domain, i.source_tx, ethereum_domain=eth_domain)
            if i.source_domain == eth_domain
            else i.source_tx
            for i in self._store.items
        }
        known_dest = {i.dest_tx for i in self._store.items if i.dest_tx}

        sol_rpc = chains["solana"].rpc_url
        sig_limit = int(os.getenv("CCTP_DISCOVER_SOL_SIG_LIMIT", "8"))
        for sig in _recent_sol_signatures(sol_exec.pubkey, sol_rpc, limit=sig_limit):
            if sig in known or sig in known_dest:
                continue
            burn = _is_sol_cctp_burn(sig, sol_rpc)
            if burn is None:
                logger.warning("Solana RPC rate limited during CCTP discover — stopping scan")
                break
            if not burn:
                continue
            _, is_new = self.enqueue(
                source_tx=sig,
                source_domain=sol_domain,
                dest_domain=eth_domain,
                intent="discovered_sol_burn",
            )
            if is_new:
                added += 1

        try:
            eth_exec = EthereumExecutor(chains["ethereum"])
            for tx_hash in _recent_eth_burn_txs(eth_exec.address, limit=15):
                norm = normalize_cctp_tx_hash(eth_domain, tx_hash, ethereum_domain=eth_domain)
                if norm in known or tx_hash in known:
                    continue
                _, is_new = self.enqueue(
                    source_tx=tx_hash,
                    source_domain=eth_domain,
                    dest_domain=sol_domain,
                    intent="discovered_eth_burn",
                )
                if is_new:
                    added += 1
        except Exception as exc:
            logger.warning("ETH burn discovery skipped (%s)", exc)

        for item in self._store.pending():
            await self._refresh_iris(client, item)
            if (
                not item.message_hex
                and item.status == CctpQueueStatus.PENDING_ATTESTATION.value
                and item.intent == "discovered_eth_burn"
            ):
                item.status = CctpQueueStatus.FAILED.value
                item.error = "no_iris_message"
                item.updated_at = datetime.now(timezone.utc).isoformat()

        self.save()
        return added

    async def _refresh_iris(self, client: httpx.AsyncClient, item: CctpPendingItem) -> None:
        if item.terminal:
            return
        item.attestation_polls += 1
        messages = await fetch_messages(client, item.source_domain, item.source_tx)
        if not messages:
            item.updated_at = datetime.now(timezone.utc).isoformat()
            return
        msg = messages[0]
        att = str(msg.get("attestation") or "")
        if not att or att == "PENDING":
            item.status = CctpQueueStatus.PENDING_ATTESTATION.value
            item.updated_at = datetime.now(timezone.utc).isoformat()
            return
        item.message_hex = str(msg.get("message") or "")
        item.attestation_hex = att
        if item.status != CctpQueueStatus.CLAIMING.value:
            item.status = CctpQueueStatus.READY.value
        item.updated_at = datetime.now(timezone.utc).isoformat()

    def _claim_already_done(self, err: str | None) -> bool:
        if not err:
            return False
        low = err.lower()
        return any(
            k in low
            for k in (
                "already",
                "already in use",
                "nonce already used",
                "message already received",
                "invalid nonce",
            )
        )

    def _fail_stale_pending(self, *, max_polls: int = 12) -> None:
        """Stop retrying burns with no Iris message when a claimed twin exists or polls exhausted."""
        eth_dom = int(self.cfg["ethereum_domain"])
        claimed_keys = {
            f"{i.source_domain}:{normalize_cctp_tx_hash(i.source_domain, i.source_tx, ethereum_domain=eth_dom)}"
            for i in self._store.items
            if i.status == CctpQueueStatus.CLAIMED.value
        }
        changed = False
        for item in self._store.pending():
            key = f"{item.source_domain}:{normalize_cctp_tx_hash(item.source_domain, item.source_tx, ethereum_domain=eth_dom)}"
            if key in claimed_keys:
                item.status = CctpQueueStatus.CLAIMED.value
                item.error = "already_claimed_duplicate"
                item.updated_at = datetime.now(timezone.utc).isoformat()
                changed = True
                continue
            if (
                item.status == CctpQueueStatus.PENDING_ATTESTATION.value
                and not item.message_hex
                and item.attestation_polls >= max_polls
            ):
                item.status = CctpQueueStatus.FAILED.value
                item.error = "iris_not_found_or_already_claimed"
                item.updated_at = datetime.now(timezone.utc).isoformat()
                changed = True
                logger.warning("CCTP giving up on stale burn %s after %s polls", item.source_tx[:18], item.attestation_polls)
        if changed:
            self.save()

    async def claim_item(
        self, client: httpx.AsyncClient, item: CctpPendingItem, *, skip_refresh: bool = False
    ) -> bool:
        if item.terminal:
            return item.status == CctpQueueStatus.CLAIMED.value

        self._resolve_claimed_siblings()
        if item.terminal:
            return item.status == CctpQueueStatus.CLAIMED.value

        if not skip_refresh or item.status != CctpQueueStatus.READY.value:
            await self._refresh_iris(client, item)
        if item.status != CctpQueueStatus.READY.value:
            return False
        if not item.message_hex or not item.attestation_hex:
            return False

        if is_dry_run():
            item.status = CctpQueueStatus.CLAIMED.value
            item.dest_tx = "dry-run-claim"
            item.updated_at = datetime.now(timezone.utc).isoformat()
            self.save()
            return True

        item.status = CctpQueueStatus.CLAIMING.value
        self.save()

        att = CctpAttestation(
            message=item.message_hex,
            attestation=item.attestation_hex,
            status="complete",
        )
        dest_tx: str | None = None
        err: str | None = None

        if item.dest_chain == "ethereum":
            chains = load_chains()
            eth_exec = EthereumExecutor(chains["ethereum"])
            dest_tx = eth_exec.receive_message(
                message_transmitter=self.cfg["ethereum_message_transmitter"],
                message_hex=att.message,
                attestation_hex=att.attestation,
            )
            if dest_tx == "already-claimed":
                item.status = CctpQueueStatus.CLAIMED.value
                item.error = "already_claimed"
                item.updated_at = datetime.now(timezone.utc).isoformat()
                self.save()
                logger.info("CCTP %s already claimed on ETH", item.source_tx)
                return True
            if not dest_tx:
                err = eth_exec.last_error or "ETH receiveMessage failed"
        elif item.dest_chain == "solana":
            import os

            chains = load_chains()
            sol_exec = SolanaExecutor(chains["solana"])
            dest_tx, err = run_receive_sol(
                message_hex=att.message,
                attestation_hex=att.attestation,
                sol_rpc=chains["solana"].rpc_url,
                sol_secret=os.getenv("SOLANA_SECRET_KEY", ""),
                sol_owner=sol_exec.pubkey,
                sol_usdc_mint=self.cfg["solana_usdc"],
                eth_domain=int(self.cfg["ethereum_domain"]),
                eth_usdc=self.cfg["ethereum_usdc"],
                iris_api=self.cfg["iris_api"].rstrip("/"),
            )
        else:
            err = f"unsupported dest domain {item.dest_domain}"

        if dest_tx and dest_tx != "already-claimed":
            item.status = CctpQueueStatus.CLAIMED.value
            item.dest_tx = dest_tx
            item.error = None
            log_tx(
                f"cctp_claim_{item.intent}",
                item.dest_chain,
                dest_tx,
                extra={"source_tx": item.source_tx, "source_domain": item.source_domain},
            )
            log_tx(
                f"cctp_burn_{item.intent}",
                "solana" if item.source_domain == int(self.cfg["solana_domain"]) else "ethereum",
                item.source_tx,
                extra={"dest_tx": dest_tx},
            )
            self.save()
            return True

        if self._claim_already_done(err):
            item.status = CctpQueueStatus.CLAIMED.value
            item.error = err
            item.updated_at = datetime.now(timezone.utc).isoformat()
            self.save()
            logger.info("CCTP %s already claimed: %s", item.source_tx, err)
            return True

        item.status = CctpQueueStatus.READY.value
        item.error = err
        item.updated_at = datetime.now(timezone.utc).isoformat()
        self.save()
        logger.error("CCTP claim failed for %s: %s", item.source_tx, err)
        return False

    async def process_once(self, client: httpx.AsyncClient) -> int:
        """Refresh and claim all ready items once. Returns number claimed."""
        claimed = 0
        stagger_ms = float(os.getenv("CCTP_IRIS_STAGGER_MS", "500"))
        for item in list(self._store.pending()):
            if item.status == CctpQueueStatus.PENDING_ATTESTATION.value:
                await self._refresh_iris(client, item)
                if stagger_ms > 0:
                    await asyncio.sleep(stagger_ms / 1000.0)
        self.save()

        for item in list(self._store.pending()):
            if item.status != CctpQueueStatus.READY.value:
                continue
            if await self.claim_item(client, item, skip_refresh=True):
                claimed += 1
        return claimed

    def _prune_false_burns(self) -> None:
        """Mark queue items that are claim/receive txs mis-detected as burns."""
        dest_sigs = {
            i.dest_tx
            for i in self._store.items
            if i.dest_tx and i.dest_tx not in ("already-claimed", "dry-run-claim")
        }
        changed = False
        for item in self._store.items:
            if item.terminal:
                continue
            if item.source_tx in dest_sigs:
                item.status = CctpQueueStatus.FAILED.value
                item.error = "not_a_burn_claim_tx"
                item.updated_at = datetime.now(timezone.utc).isoformat()
                changed = True
                logger.info("CCTP pruned false burn %s", item.source_tx[:16])
        if changed:
            self.save()

    async def run_until_empty(
        self,
        client: httpx.AsyncClient,
        *,
        interval_sec: float = 30.0,
        max_rounds: int = 120,
        discover_first: bool = True,
    ) -> dict[str, Any]:
        """Discover pending burns, poll Iris, claim until queue has no actionable items."""
        if discover_first:
            await self.discover(client)
        self._prune_false_burns()
        self._coalesce_duplicates(self._store)
        self._resolve_claimed_siblings()
        self.save()

        max_stale_polls = int(os.getenv("CCTP_MAX_STALE_POLLS", "12"))

        rounds = 0
        total_claimed = 0
        while rounds < max_rounds:
            rounds += 1
            pending = self._store.pending()
            if not pending:
                logger.info("CCTP queue empty after %s rounds", rounds)
                break

            awaiting = sum(1 for i in pending if i.status == CctpQueueStatus.PENDING_ATTESTATION.value)
            ready = sum(1 for i in pending if i.status == CctpQueueStatus.READY.value)
            logger.info(
                "CCTP queue round %s: %s pending (%s awaiting attestation, %s ready)",
                rounds,
                len(pending),
                awaiting,
                ready,
            )

            claimed = await self.process_once(client)
            total_claimed += claimed
            self._fail_stale_pending(max_polls=max_stale_polls)
            self._resolve_claimed_siblings()

            still = self._store.pending()
            if not still:
                break
            if claimed == 0 and ready > 0:
                # All ready items failed — likely already claimed on-chain
                for item in still:
                    if item.status == CctpQueueStatus.READY.value and self._claim_already_done(item.error):
                        item.status = CctpQueueStatus.CLAIMED.value
                        item.updated_at = datetime.now(timezone.utc).isoformat()
                self.save()
                still = self._store.pending()
                if not still:
                    break
            if claimed == 0 and ready == 0:
                for item in still:
                    if (
                        item.status == CctpQueueStatus.PENDING_ATTESTATION.value
                        and not item.message_hex
                        and rounds >= 5
                        and item.intent.startswith("discovered_")
                    ):
                        item.status = CctpQueueStatus.FAILED.value
                        item.error = "no_iris_message"
                        item.updated_at = datetime.now(timezone.utc).isoformat()
                self.save()
                still = self._store.pending()
                if not still:
                    break
                logger.info("Waiting for attestation (%s items); sleep %.0fs", len(still), interval_sec)
            elif claimed == 0:
                logger.info("No claims this round; sleep %.0fs", interval_sec)
            await asyncio.sleep(interval_sec)

        return {
            "rounds": rounds,
            "claimed": total_claimed,
            "remaining": len(self._store.pending()),
            "items": [asdict(i) for i in self._store.items],
        }


def _recent_sol_signatures(pubkey: str, rpc_url: str, limit: int = 40) -> list[str]:
    data = post_json_rpc_sync(
        rpc_url,
        "getSignaturesForAddress",
        [pubkey, {"limit": limit}],
        provider="solana_rpc",
    )
    rows = data.get("result") or []
    return [r["signature"] for r in rows if not r.get("err")]


def _rpc_rate_limited(data: dict[str, Any]) -> bool:
    err = data.get("error")
    if not err:
        return False
    if isinstance(err, dict) and err.get("code") in (-32005, -32429):
        return True
    return "429" in str(err).lower() or "too many requests" in str(err).lower()


def _is_sol_cctp_burn(signature: str, rpc_url: str) -> bool | None:
    data = post_json_rpc_sync(
        rpc_url,
        "getTransaction",
        [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        provider="solana_rpc",
    )
    if _rpc_rate_limited(data):
        return None
    tx = data.get("result")
    if not tx:
        return False
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys") or []
    has_cctp = any(
        (k if isinstance(k, str) else k.get("pubkey", "")) == SOL_CCTP_PROGRAM for k in keys
    )
    if not has_cctp:
        return False
    logs = tx.get("meta", {}).get("logMessages") or []
    log_text = " ".join(logs).lower()
    if any(k in log_text for k in ("receivemessage", "message received", "receive_message")):
        return False
    return any(k in log_text for k in ("burn", "depositforburn", "deposit for burn"))


def _recent_eth_burn_txs(address: str, limit: int = 20) -> list[str]:
    import httpx as hx

    from src.quotes.sync_throttle import retry_backoff_sec, sync_throttle

    url = f"https://eth.blockscout.com/api/v2/addresses/{address}/transactions"
    max_attempts = int(os.getenv("RPC_RETRY_MAX", "5"))
    for attempt in range(max_attempts):
        try:
            sync_throttle("blockscout")
            resp = hx.get(url, timeout=30)
            if resp.status_code in (429, 502, 503, 504) and attempt + 1 < max_attempts:
                time.sleep(retry_backoff_sec(attempt))
                continue
            if resp.status_code != 200:
                return []
            out: list[str] = []
            for t in resp.json().get("items") or []:
                if t.get("status") != "ok":
                    continue
                to_addr = (t.get("to") or {}).get("hash", "")
                method = (t.get("method") or "").lower()
                if to_addr.lower() == ETH_TOKEN_MESSENGER.lower() or "depositforburn" in method:
                    h = t.get("hash")
                    if h:
                        out.append(h if h.startswith("0x") else f"0x{h}")
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            if attempt + 1 >= max_attempts:
                logger.debug("ETH burn scan failed: %s", exc)
                return []
            time.sleep(retry_backoff_sec(attempt))
    return []
