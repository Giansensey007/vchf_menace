"""Treasury prepare_for_direction: 30 VCHF order min vs withdraw-only sizing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import BotConfig, load_chains, load_tokens
from src.treasury.manager import TreasuryManager, TreasurySnapshot
from src.vnx.bridge import VCHF_WITHDRAW_FEE_BUFFER
from src.vnx.trading import VCHF_MIN_ORDER, VCHF_USDC_QTY_DECIMALS, _round_down


def _bot_cfg(**overrides) -> BotConfig:
    base = dict(
        poll_interval_sec=60,
        min_profit_usd=5,
        max_trade_vchf=2000,
        min_trade_vchf=200,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[10],
        slippage_bps=50,
        quote_freshness_sec=30,
        peg_min=0.98,
        peg_max=1.02,
        vnx_bridge_poll_sec=1,
        vnx_bridge_timeout_sec=5,
        base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0,
        vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=False,
        enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0,
        cctp_fee_usd=1.5,
        jit_withdraw=False,
        platform_vchf_only=False,
    )
    base.update(overrides)
    return BotConfig(**base)


@pytest.fixture
def treasury() -> TreasuryManager:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    return TreasuryManager(chains, token, _bot_cfg())


async def _prepare(
    treasury: TreasuryManager,
    *,
    platform_vchf: float,
    platform_usdc: float,
    direction: str = "vnx_to_celo",
    size: float = 31.0,
    celo_usdt: float = 500.0,
    sol_usdc: float = 500.0,
):
    snap = TreasurySnapshot(
        platform_vchf=platform_vchf,
        platform_usdc=platform_usdc,
        celo_usdt=celo_usdt,
        sol_usdc=sol_usdc,
    )
    with patch.object(treasury, "snapshot", AsyncMock(return_value=snap)):
        with patch.object(treasury, "consolidate_vchf_to_platform", AsyncMock(return_value=0.0)):
            return await treasury.prepare_for_direction(direction, size)


@pytest.mark.asyncio
async def test_prepare_keeps_requested_size_when_platform_sufficient(treasury):
    prep = await _prepare(treasury, platform_vchf=50.0, platform_usdc=1000.0, size=31.0)
    assert prep.ready
    assert prep.size_vchf == 31.0
    assert prep.notes[-1] == "ready"
    assert not any("withdraw-only" in n for n in prep.notes)
    assert not any("will buy" in n for n in prep.notes)


@pytest.mark.asyncio
async def test_prepare_withdraw_only_below_order_min(treasury):
    """10 VCHF on platform — below 30 order min but withdrawable after fee buffer."""
    prep = await _prepare(treasury, platform_vchf=10.0, platform_usdc=100.0, size=31.0)
    expected = _round_down(10.0 - VCHF_WITHDRAW_FEE_BUFFER, VCHF_USDC_QTY_DECIMALS)
    assert prep.ready
    assert prep.size_vchf == expected
    assert expected < VCHF_MIN_ORDER
    assert any("withdraw-only size" in n for n in prep.notes)


@pytest.mark.asyncio
async def test_prepare_buys_order_minimum_when_no_withdrawable(treasury):
    prep = await _prepare(treasury, platform_vchf=0.5, platform_usdc=100.0, size=31.0)
    assert prep.ready
    assert prep.size_vchf == VCHF_MIN_ORDER
    assert any("will buy 30 VCHF on platform (order minimum)" in n for n in prep.notes)


@pytest.mark.asyncio
async def test_prepare_fails_when_platform_underfunded(treasury):
    prep = await _prepare(treasury, platform_vchf=0.5, platform_usdc=10.0, size=31.0)
    assert not prep.ready
    assert any("platform short" in n for n in prep.notes)
    assert any("platform order min" in n for n in prep.notes)


@pytest.mark.asyncio
async def test_prepare_vnx_to_solana_same_withdraw_logic(treasury):
    prep = await _prepare(
        treasury,
        platform_vchf=12.0,
        platform_usdc=50.0,
        direction="vnx_to_solana",
        size=31.0,
    )
    expected = _round_down(12.0 - VCHF_WITHDRAW_FEE_BUFFER, VCHF_USDC_QTY_DECIMALS)
    assert prep.ready
    assert prep.size_vchf == expected
    assert any("withdraw-only size" in n for n in prep.notes)


def test_route_matrix_size_flag_wiring():
    """execute_route_matrix force-exec steps use _ROUTE_SIZE from --size."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "execute_route_matrix.py").read_text()
    assert "TEST_VCHF = 31.0" in src
    assert "_ROUTE_SIZE = args.size" in src
    assert "_ROUTE_SIZE" in src and '_force_exec("vnx_to_' in src
