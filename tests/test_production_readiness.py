"""Production funding readiness checks."""
from unittest.mock import AsyncMock, patch

import pytest

from src.treasury.readiness import FundingTarget, format_report, production_targets


def test_production_targets_loaded():
    t = production_targets("production")
    assert t["platform_vchf"] == 200
    assert t["base_usdc"] == 250
    assert t["sol_usdc"] == 250


def test_funding_target_gap():
    row = FundingTarget("base_usdc", "Base USDT", 250, 100, "USDT")
    assert row.gap == 150
    assert not row.ok


def test_format_report_underfunded():
    rows = [
        FundingTarget("platform_vchf", "VNX VCHF", 200, 50, "VCHF"),
        FundingTarget("base_usdc", "Base USDT", 250, 260, "USDT"),
    ]
    text = format_report(rows, {})
    assert "UNDER-FUNDED" in text
    assert "NEED +150" in text


@pytest.mark.asyncio
async def test_funding_report_mock():
    from src.treasury.readiness import funding_report

    fake = {
        "platform_vchf": 200,
        "platform_usdc": 250,
        "base_usdc": 100,
        "sol_usdc": 250,
        "eth_native": 0.02,
        "eth_usdc": 50,
        "eth_usdt": 50,
        "base_native": 1.0,
        "sol_native": 0.1,
    }
    with patch("src.treasury.readiness.collect_balances", new_callable=AsyncMock) as mock_bal:
        mock_bal.return_value = fake
        rows, balances = await funding_report("production")
    assert balances["platform_vchf"] == 200
    assert any(r.key == "base_usdc" and not r.ok for r in rows)
