"""Loop selector: best-loop probe + size search, profit/floor gating (VCHF)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.scanner.loop_selector import select_best_loop
from src.scanner.loop_simulator import LoopSimulation
from src.scanner.routes import LOOP1_OUTBOUND, LOOP3_CROSS, LoopSpec

MOD = "src.scanner.loop_selector"

TOKEN = TokenConfig(
    symbol="VCHF", decimals=18, chain_decimals={"solana": 9},
    chains={"celo": "0xcelo", "base": "0xbase", "solana": "solV", "vnx": "VCHF", "ethereum": "0xeth"},
)
CHAINS: dict[str, ChainConfig] = {}

LOOPS = (
    LoopSpec(LOOP1_OUTBOUND, "VCHF", "celo"),
    LoopSpec(LOOP1_OUTBOUND, "VCHF", "base"),
    LoopSpec(LOOP3_CROSS, "VCHF", "base", "solana"),
)


def _cfg(*, min_profit_usd: float = 5.0) -> BotConfig:
    return BotConfig(
        poll_interval_sec=60, min_profit_usd=min_profit_usd, min_trade_vchf=200, max_trade_vchf=2000,
        sizing_coarse_step=100, max_sizing_quotes=3, probe_sizes=[200], slippage_bps=50,
        quote_freshness_sec=30, peg_min=0.98, peg_max=1.02, vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600, celo_gas_usd_estimate=0.25, base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05, vnx_bridge_fee_usd=1.0, vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5, enable_vnx_arb_routes=True, enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0, eth_gas_usd_estimate=2.0, platform_vchf_only=True,
        treasury_vchf_home="platform", jit_withdraw=True,
    )


def _fake_sim(profits: dict[str, float], *, size_bonus: float = 0.0):
    async def fake(client, chains, token, cfg, loop, size):
        p = profits.get(loop.key, -1.0)
        if p > -1.0:
            p = p + size * size_bonus
        sim = LoopSimulation(loop_key=loop.key, family=loop.family, token="VCHF", size=size)
        sim.net_profit_usd = p
        sim.net_token = p
        sim.token_out = size + p
        sim.ref_price = 1.0
        sim.fees_usd = 1.0
        sim.profitable = p > 0
        sim.floors_ok = True
        return sim

    return fake


async def _run(cfg, fake):
    with (
        patch(f"{MOD}.active_loops", new=MagicMock(return_value=LOOPS)),
        patch(f"{MOD}.simulate_loop", new=fake),
    ):
        return await select_best_loop(MagicMock(), CHAINS, TOKEN, cfg)


@pytest.mark.asyncio
async def test_picks_highest_profit_loop():
    fake = _fake_sim({"loop1_outbound:celo": 8.0, "loop1_outbound:base": 15.0, "loop3_cross:base->solana": 3.0})
    sel = await _run(_cfg(), fake)
    assert sel.best is not None
    assert sel.best.loop_key == "loop1_outbound:base"


@pytest.mark.asyncio
async def test_min_profit_gate_excludes_low_profit():
    fake = _fake_sim({"loop1_outbound:celo": 8.0, "loop1_outbound:base": 15.0, "loop3_cross:base->solana": 3.0})
    sel = await _run(_cfg(min_profit_usd=10.0), fake)
    assert sel.best is not None
    assert sel.best.loop_key == "loop1_outbound:base"


@pytest.mark.asyncio
async def test_none_profitable_returns_no_selection():
    fake = _fake_sim({"loop1_outbound:celo": -2.0, "loop1_outbound:base": -1.0, "loop3_cross:base->solana": -5.0})
    sel = await _run(_cfg(), fake)
    assert sel.best is None
    assert "no profitable loop" in sel.reason


@pytest.mark.asyncio
async def test_size_search_picks_largest_profitable_size():
    fake = _fake_sim({"loop1_outbound:base": 10.0, "loop1_outbound:celo": 1.0}, size_bonus=0.01)
    sel = await _run(_cfg(), fake)
    assert sel.best is not None
    assert sel.best.loop_key == "loop1_outbound:base"
    assert sel.best.size == 400.0  # grid = [200, 300, 400]
