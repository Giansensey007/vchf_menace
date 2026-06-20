"""In-flight ledger reconcile and duplicate-withdraw guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.treasury.in_flight import (
    InFlightLedger,
    KIND_VNX_WITHDRAW,
    STATUS_PENDING,
    STATUS_SETTLED,
    parse_vnx_withdrawals,
)
from src.vnx.bridge import VnxBridge


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "in_flight.jsonl"


def test_reconcile_settles_vnx_withdraw_on_base_arrival(ledger_path: Path) -> None:
    ledger = InFlightLedger("VCHF", ledger_path)
    ledger.log_vnx_withdraw(
        9.55,
        "BASE",
        "base-hot",
        "vnx_to_base",
        txids=["wd-1"],
        baseline_base_token=0.0,
        baseline_celo_token=0.0,
        baseline_platform_token=50.0,
    )
    active = ledger.reconcile(platform_token=40.0, celo_token=0.0, base_token=0.0, sol_token=0.0)
    assert len(active) == 1
    assert active[0].status == STATUS_PENDING

    ledger.reconcile(platform_token=40.0, celo_token=0.0, base_token=9.5, sol_token=0.0)
    records = ledger.read_all()
    settled = [r for r in records if r.kind == KIND_VNX_WITHDRAW]
    assert settled[0].status == STATUS_SETTLED


def test_reconcile_settles_vnx_withdraw_on_celo_arrival(ledger_path: Path) -> None:
    ledger = InFlightLedger("VCHF", ledger_path)
    ledger.log_vnx_withdraw(
        9.55,
        "CELO",
        "celo-hot",
        "vnx_to_celo",
        txids=["wd-celo"],
        baseline_celo_token=0.0,
        baseline_base_token=0.0,
    )
    ledger.reconcile(platform_token=40.0, celo_token=9.5, base_token=0.0, sol_token=0.0)
    records = ledger.read_all()
    assert records[0].status == STATUS_SETTLED


def test_parse_vnx_withdrawals_api_shape() -> None:
    api = {
        "withdrawals": [
            {
                "asset": "VCHF",
                "quantity": 9.55,
                "blockchain": "BASE",
                "destination": "base-hot",
                "status": "pending",
                "txid": "abc123",
            },
            {
                "asset": "VCHF",
                "quantity": 5.0,
                "blockchain": "SOL",
                "status": "completed",
            },
        ]
    }
    parsed = parse_vnx_withdrawals(api, "VCHF")
    assert len(parsed) == 1
    assert parsed[0].quantity == 9.55
    assert parsed[0].blockchain == "BASE"


def test_reconcile_merges_api_pending_withdraw(ledger_path: Path) -> None:
    ledger = InFlightLedger("VCHF", ledger_path)
    from src.treasury.in_flight import PendingVnxWithdraw

    api = [
        PendingVnxWithdraw(
            asset="VCHF",
            quantity=9.55,
            blockchain="BASE",
            destination="base-hot",
            status="pending",
            txid="api-tx",
        )
    ]
    active = ledger.reconcile(
        platform_token=10.0, celo_token=0.0, base_token=0.0, sol_token=0.0, api_withdrawals=api
    )
    assert len(active) == 1
    assert active[0].extra.get("source") == "vnx_api"
    assert active[0].extra.get("baseline_base_token") == 0.0
    assert active[0].extra.get("baseline_celo_token") == 0.0


def test_purge_stale_pending_marks_old_records(ledger_path: Path) -> None:
    ledger = InFlightLedger("VCHF", ledger_path)
    ledger.log_vnx_deposit(50.0, "SOL", "vnx_to_solana", "0xdep", baseline_platform_token=0.0)
    records = ledger.read_all()
    records[0].created_at = "2020-01-01T00:00:00+00:00"
    ledger._rewrite(records)
    assert ledger.purge_stale_pending(max_age_hours=1) == 1
    assert ledger.read_all()[0].status == "failed"


@pytest.mark.asyncio
async def test_bridge_skips_duplicate_withdraw_when_pending(ledger_path: Path, monkeypatch) -> None:
    ledger = InFlightLedger("VCHF", ledger_path)
    ledger.log_vnx_withdraw(
        9.55,
        "BASE",
        "base-hot",
        "vnx_to_base",
        txids=["existing"],
        baseline_base_token=0.0,
        baseline_celo_token=0.0,
    )

    from src.config_loader import load_bot_config

    bridge = VnxBridge(load_bot_config())
    bridge._ledger = ledger

    withdraw_called = False

    class FakeVnx:
        async def account_balance(self):
            return {"balances": [{"asset": "VCHF", "available_balance": 50}]}

        async def account_balance_resilient(self):
            return await self.account_balance()

        def vchf_balance(self, bal):
            return 50.0

        async def withdraw(self, *args, **kwargs):
            nonlocal withdraw_called
            withdraw_called = True
            return {"txids": ["new"]}

    class Ctx:
        async def __aenter__(self):
            return FakeVnx()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr("src.vnx.bridge.VnxClient", lambda: Ctx())
    monkeypatch.setattr("src.vnx.bridge.is_dry_run", lambda: False)

    result = await bridge.bridge_vchf(
        direction="vnx_to_base",
        quantity=9.55,
        source_blockchain="BASE",
        dest_blockchain="BASE",
        dest_label="base-hot",
        deposit_tx_builder=lambda _a: None,
        withdraw_only=True,
    )
    assert result.success
    assert not withdraw_called
    assert "existing" in (result.withdraw_txids or [])
