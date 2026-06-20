from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import data_dir

logger = logging.getLogger(__name__)


def in_flight_path() -> Path:
    return data_dir() / "in_flight.jsonl"

STATUS_PENDING = "pending"
STATUS_SETTLED = "settled"
STATUS_FAILED = "failed"

KIND_VNX_WITHDRAW = "vnx_withdraw"
KIND_VNX_DEPOSIT = "vnx_deposit"
KIND_CCTP_BURN = "cctp_burn"
KIND_WORMHOLE_BURN = "wormhole_burn"

_BLOCKCHAIN_ALIASES = {
    "base": "BASE",
    "BASE": "BASE",
    "solana": "SOL",
    "sol": "SOL",
    "SOL": "SOL",
    "ethereum": "ETH",
    "eth": "ETH",
    "ETH": "ETH",
}


@dataclass
class PendingVnxWithdraw:
    asset: str
    quantity: float
    blockchain: str
    destination: str
    status: str
    txid: str | None = None
    created_at: str = ""


@dataclass
class InFlightRecord:
    id: str
    kind: str
    asset: str
    quantity: float
    blockchain: str
    destination: str = ""
    direction: str = ""
    status: str = STATUS_PENDING
    txids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    settled_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_blockchain(blockchain: str) -> str:
    return _BLOCKCHAIN_ALIASES.get(blockchain, blockchain.upper())


def read_on_chain_token_balances(chains: Any, token: Any) -> tuple[float, float]:
    """Return (base_token, sol_token) UI balances for reconcile baselines."""
    from src.config_loader import token_decimals
    from src.execution.base import BaseExecutor
    from src.execution.solana import SolanaExecutor
    from src.quotes.types import to_human
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    base = BaseExecutor(chains["base"])
    dec = token_decimals(token, "base")
    base_bal = float(to_human(base.balance_erc20(token.chains["base"]), dec))
    sol = SolanaExecutor(chains["solana"])
    sdec = token_decimals(token, "solana")
    try:
        mint = Pubkey.from_string(token.chains["solana"])
        ata = get_associated_token_address(sol.keypair.pubkey(), mint)
        sol_bal = sol.token_balance_ui(ata)
    except Exception:
        sol_bal = 0.0
    return base_bal, sol_bal


def parse_vnx_withdrawals(api_resp: dict[str, Any] | None, token_asset: str) -> list[PendingVnxWithdraw]:
    """Parse queryWithdrawals / transfers API response; tolerate unknown shapes."""
    if not api_resp or api_resp.get("result") == "error":
        return []
    rows = (
        api_resp.get("withdrawals")
        or api_resp.get("withdraws")
        or api_resp.get("transfers")
        or api_resp.get("items")
        or []
    )
    out: list[PendingVnxWithdraw] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset") or row.get("currency") or "")
        if asset and asset != token_asset:
            continue
        qty = float(row.get("quantity") or row.get("amount") or row.get("qty") or 0)
        if qty <= 0:
            continue
        bc = _norm_blockchain(str(row.get("blockchain") or row.get("chain") or ""))
        dest = str(row.get("destination") or row.get("label") or row.get("address") or "")
        status = str(row.get("status") or row.get("state") or STATUS_PENDING).lower()
        txid = row.get("txid") or row.get("tx_id") or row.get("txHash")
        if isinstance(txid, list):
            txid = txid[0] if txid else None
        txid = str(txid) if txid else None
        created = str(row.get("created_at") or row.get("timestamp") or row.get("time") or "")
        if status in ("completed", "done", "settled", "success"):
            continue
        out.append(
            PendingVnxWithdraw(
                asset=asset or token_asset,
                quantity=qty,
                blockchain=bc,
                destination=dest,
                status=status,
                txid=txid,
                created_at=created,
            )
        )
    return out


class InFlightLedger:
    """Persistent ledger for in-flight VNX withdraws, deposits, CCTP/Wormhole burns."""

    def __init__(self, token_asset: str, path: Path | None = None) -> None:
        self.token_asset = token_asset
        self.path = path or in_flight_path()

    def read_all(self) -> list[InFlightRecord]:
        if not self.path.exists():
            return []
        records: list[InFlightRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(InFlightRecord(**data))
            except Exception as exc:
                logger.warning("Skip corrupt in_flight line: %s", exc)
        return records

    def _append(self, rec: InFlightRecord) -> InFlightRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        logger.info(
            "In-flight %s %.4f %s → %s (%s)",
            rec.kind,
            rec.quantity,
            rec.asset,
            rec.blockchain,
            rec.direction or rec.destination,
        )
        return rec

    def _new_record(
        self,
        kind: str,
        quantity: float,
        blockchain: str,
        *,
        destination: str = "",
        direction: str = "",
        txids: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> InFlightRecord:
        now = _now()
        return InFlightRecord(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            asset=self.token_asset,
            quantity=quantity,
            blockchain=_norm_blockchain(blockchain),
            destination=destination,
            direction=direction,
            status=STATUS_PENDING,
            txids=list(txids or []),
            created_at=now,
            updated_at=now,
            extra=extra or {},
        )

    def log_vnx_withdraw(
        self,
        quantity: float,
        blockchain: str,
        destination: str,
        direction: str,
        txids: list | None = None,
        *,
        baseline_base_token: float | None = None,
        baseline_sol_token: float | None = None,
        baseline_platform_token: float | None = None,
    ) -> InFlightRecord:
        extra: dict[str, Any] = {}
        if baseline_base_token is not None:
            extra["baseline_base_token"] = baseline_base_token
        if baseline_sol_token is not None:
            extra["baseline_sol_token"] = baseline_sol_token
        if baseline_platform_token is not None:
            extra["baseline_platform_token"] = baseline_platform_token
        rec = self._new_record(
            KIND_VNX_WITHDRAW,
            quantity,
            blockchain,
            destination=destination,
            direction=direction,
            txids=[str(t) for t in (txids or []) if t],
            extra=extra,
        )
        return self._append(rec)

    def log_vnx_deposit(
        self,
        quantity: float,
        blockchain: str,
        direction: str,
        deposit_tx: str | None = None,
        *,
        baseline_platform_token: float | None = None,
    ) -> InFlightRecord:
        extra: dict[str, Any] = {}
        if deposit_tx:
            extra["deposit_tx"] = deposit_tx
        if baseline_platform_token is not None:
            extra["baseline_platform_token"] = baseline_platform_token
        rec = self._new_record(
            KIND_VNX_DEPOSIT,
            quantity,
            blockchain,
            direction=direction,
            txids=[deposit_tx] if deposit_tx else [],
            extra=extra,
        )
        return self._append(rec)

    def log_cctp_burn(
        self,
        source_tx: str,
        dest_chain: str,
        intent: str = "cctp_bridge",
        quantity: float = 0.0,
    ) -> InFlightRecord:
        rec = self._new_record(
            KIND_CCTP_BURN,
            quantity,
            dest_chain,
            direction=intent,
            txids=[source_tx],
            extra={"source_tx": source_tx, "dest_chain": dest_chain},
        )
        return self._append(rec)

    def log_wormhole_burn(
        self,
        source_tx: str,
        source_chain: str,
        dest_chain: str,
        intent: str = "wormhole_usdt",
        quantity: float = 0.0,
    ) -> InFlightRecord:
        rec = self._new_record(
            KIND_WORMHOLE_BURN,
            quantity,
            dest_chain,
            direction=intent,
            txids=[source_tx],
            extra={"source_chain": source_chain, "dest_chain": dest_chain},
        )
        return self._append(rec)

    def active(self) -> list[InFlightRecord]:
        return [r for r in self.read_all() if r.status == STATUS_PENDING]

    def pending_vnx_withdraws(self) -> list[InFlightRecord]:
        return [r for r in self.active() if r.kind == KIND_VNX_WITHDRAW]

    def pending_for_blockchain(self, blockchain: str) -> list[InFlightRecord]:
        bc = _norm_blockchain(blockchain)
        return [r for r in self.pending_vnx_withdraws() if r.blockchain == bc]

    def total_pending_to_blockchain(self, blockchain: str) -> float:
        return sum(r.quantity for r in self.pending_for_blockchain(blockchain))

    def mark_failed(self, record_id: str, reason: str) -> None:
        self._update_status(record_id, STATUS_FAILED, extra_note=reason)

    def purge_stale_pending(self, max_age_hours: float = 48.0) -> int:
        """Mark pending records older than max_age_hours as failed (ops cleanup)."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        records = self.read_all()
        changed = False
        count = 0
        for rec in records:
            if rec.status != STATUS_PENDING:
                continue
            try:
                created = datetime.fromisoformat(rec.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if created < cutoff:
                rec.status = STATUS_FAILED
                rec.extra["note"] = f"stale pending >{max_age_hours:.0f}h"
                rec.updated_at = _now()
                changed = True
                count += 1
        if changed:
            self._rewrite(records)
        return count

    def mark_settled(self, record_id: str) -> None:
        self._update_status(record_id, STATUS_SETTLED, settled_at=_now())

    def _update_status(
        self,
        record_id: str,
        status: str,
        settled_at: str | None = None,
        extra_note: str | None = None,
    ) -> None:
        records = self.read_all()
        updated = False
        for rec in records:
            if rec.id != record_id or rec.status != STATUS_PENDING:
                continue
            rec.status = status
            rec.updated_at = _now()
            if settled_at:
                rec.settled_at = settled_at
            if extra_note:
                rec.extra["note"] = extra_note
            updated = True
            break
        if updated:
            self._rewrite(records)

    def _rewrite(self, records: list[InFlightRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(asdict(r), ensure_ascii=False) for r in records]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def reconcile(
        self,
        *,
        platform_token: float,
        base_token: float,
        sol_token: float,
        api_withdrawals: list[PendingVnxWithdraw] | None = None,
    ) -> list[InFlightRecord]:
        """Mark settled when balances reflect arrival; merge API pending withdrawals."""
        records = self.read_all()
        changed = False
        for rec in records:
            if rec.status != STATUS_PENDING:
                continue
            if rec.kind == KIND_VNX_WITHDRAW:
                bc = rec.blockchain
                baseline_base = rec.extra.get("baseline_base_token")
                baseline_sol = rec.extra.get("baseline_sol_token")
                if bc == "BASE" and baseline_base is not None:
                    if base_token >= float(baseline_base) + rec.quantity * 0.9:
                        rec.status = STATUS_SETTLED
                        rec.settled_at = _now()
                        rec.updated_at = rec.settled_at
                        changed = True
                elif bc == "SOL" and baseline_sol is not None:
                    if sol_token >= float(baseline_sol) + rec.quantity * 0.9:
                        rec.status = STATUS_SETTLED
                        rec.settled_at = _now()
                        rec.updated_at = rec.settled_at
                        changed = True
            elif rec.kind == KIND_VNX_DEPOSIT:
                baseline_plat = rec.extra.get("baseline_platform_token")
                if baseline_plat is not None and platform_token >= float(baseline_plat) + rec.quantity * 0.9:
                    rec.status = STATUS_SETTLED
                    rec.settled_at = _now()
                    rec.updated_at = rec.settled_at
                    changed = True
            elif rec.kind in (KIND_CCTP_BURN, KIND_WORMHOLE_BURN):
                if self._reconcile_bridge_queue_item(rec):
                    changed = True

        if api_withdrawals:
            for api_w in api_withdrawals:
                if not any(
                    r.kind == KIND_VNX_WITHDRAW
                    and r.status == STATUS_PENDING
                    and r.blockchain == api_w.blockchain
                    and abs(r.quantity - api_w.quantity) < 0.05
                    for r in records
                ):
                    extra: dict[str, Any] = {"source": "vnx_api"}
                    if api_w.txid:
                        extra["api_txid"] = api_w.txid
                    new_rec = self._new_record(
                        KIND_VNX_WITHDRAW,
                        api_w.quantity,
                        api_w.blockchain,
                        destination=api_w.destination,
                        direction="api_pending",
                        txids=[api_w.txid] if api_w.txid else [],
                        extra=extra,
                    )
                    if api_w.created_at:
                        new_rec.created_at = api_w.created_at
                    records.append(new_rec)
                    changed = True

        if changed:
            self._rewrite(records)
        return self.active()

    def _reconcile_bridge_queue_item(self, rec: InFlightRecord) -> bool:
        try:
            if rec.kind == KIND_CCTP_BURN:
                from src.bridge.cctp_queue import CctpClaimQueue, CctpQueueStatus

                queue = CctpClaimQueue()
                source_tx = rec.extra.get("source_tx") or (rec.txids[0] if rec.txids else "")
                for item in queue._store.items:
                    if item.source_tx == source_tx:
                        if item.status == CctpQueueStatus.CLAIMED.value:
                            rec.status = STATUS_SETTLED
                            rec.settled_at = _now()
                            rec.updated_at = rec.settled_at
                            return True
                        if item.status == CctpQueueStatus.FAILED.value:
                            rec.status = STATUS_FAILED
                            rec.extra["note"] = item.error or "cctp failed"
                            rec.updated_at = _now()
                            return True
                        break
            elif rec.kind == KIND_WORMHOLE_BURN:
                from src.bridge.wormhole_queue import WormholeClaimQueue, WormholeQueueStatus

                queue = WormholeClaimQueue()
                source_tx = (rec.txids[0] if rec.txids else "").lower()
                for item in queue._store.items:
                    if item.source_tx.lower() == source_tx:
                        if item.status == WormholeQueueStatus.CLAIMED.value:
                            rec.status = STATUS_SETTLED
                            rec.settled_at = _now()
                            rec.updated_at = rec.settled_at
                            return True
                        if item.status == WormholeQueueStatus.FAILED.value:
                            rec.status = STATUS_FAILED
                            rec.extra["note"] = item.error or "wormhole failed"
                            rec.updated_at = _now()
                            return True
                        break
        except Exception as exc:
            logger.debug("Bridge queue reconcile skip: %s", exc)
        return False

    def format_summary(self) -> str:
        active = self.active()
        if not active:
            return "in-flight: none"
        parts = []
        for r in active:
            tx = r.txids[0] if r.txids else ""
            parts.append(
                f"{r.kind} {r.quantity:.2f} {r.asset}→{r.blockchain}"
                f" since={r.created_at[:19]} tx={tx}"
            )
        return "in-flight: " + "; ".join(parts)

    def format_audit_block(self) -> str:
        lines = ["--- In-flight / pending ---"]
        active = self.active()
        if not active:
            lines.append("  (none)")
        else:
            for r in active:
                tx = ", ".join(r.txids) if r.txids else "n/a"
                lines.append(
                    f"  {r.kind}: {r.quantity:.4f} {r.asset} blockchain={r.blockchain} "
                    f"dest={r.destination} status={r.status} since={r.created_at[:19]} tx={tx}"
                )
        api_pending = [r for r in active if r.extra.get("source") == "vnx_api"]
        if api_pending:
            lines.append(f"  VNX API pending withdrawals: {len(api_pending)}")
        return "\n".join(lines)


def format_treasury_balance_line(
    snap: Any,
    token_field: str,
    *,
    pending_vnx_withdraws: list[PendingVnxWithdraw] | None = None,
    in_flight_summary: str | None = None,
) -> str:
    """Compact one-line balance summary for poll cycles."""
    plat = getattr(snap, f"platform_{token_field}", 0.0)
    base_t = getattr(snap, f"base_{token_field}", 0.0)
    sol_t = getattr(snap, f"sol_{token_field}", 0.0)
    usdc = getattr(snap, "platform_usdc", 0.0)
    chf = getattr(snap, "platform_chf", 0.0)
    line = (
        f"Balances: plat {token_field.upper()}={plat:.2f} USDC={usdc:.2f} CHF={chf:.2f} | "
        f"Base {token_field.upper()}={base_t:.4f} USDT={snap.base_usdc:.2f} | "
        f"Sol {token_field.upper()}={sol_t:.4f} USDC={snap.sol_usdc:.2f}"
    )
    if pending_vnx_withdraws:
        pend = ", ".join(
            f"{w.quantity:.2f} {w.asset}→{w.blockchain}" for w in pending_vnx_withdraws[:3]
        )
        line += f" | pending VNX withdraw: {pend}"
    if in_flight_summary and "none" not in in_flight_summary:
        line += f" | {in_flight_summary}"
    return line
