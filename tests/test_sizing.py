from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import BotConfig
from src.scanner.sizing import search_profitable_size


@pytest.fixture
def bot_cfg():
    return BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
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
        close_loop_after_cycle=False,
    )


def _sim(net: float, error: str | None = None):
    from src.scanner.simulator import CycleSimulation

    return CycleSimulation(
        direction="base_to_solana",
        buy_chain="base",
        sell_chain="solana",
        size_vchf=100,
        stable_in_usd=100,
        stable_out_usd=100 + net,
        token_mid=100,
        net_profit_usd=net,
        profitable=net > 0,
        error=error,
    )


@pytest.mark.asyncio
async def test_search_continues_when_both_ends_unprofitable(bot_cfg):
    with patch("src.scanner.sizing.simulate_direction", new_callable=AsyncMock) as mock:
        mock.side_effect = lambda *_a, **_k: _sim(-2)
        result = await search_profitable_size(None, {}, None, bot_cfg, "base_to_solana")
    assert result is None
    assert mock.call_count >= 3  # endpoints + at least one midpoint probe


@pytest.mark.asyncio
async def test_search_finds_profitable_at_max(bot_cfg):
    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        return _sim(10 if size >= 2000 else -2)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, bot_cfg, "base_to_solana")
    assert result is not None
    assert result.size_vchf == 2000
    assert result.simulation.net_profit_usd == 10


@pytest.mark.asyncio
async def test_search_respects_max_quotes(bot_cfg):
    bot_cfg = BotConfig(
        **{**bot_cfg.__dict__, "max_sizing_quotes": 3, "sizing_coarse_step": 50}
    )
    with patch("src.scanner.sizing.simulate_direction", new_callable=AsyncMock) as mock:
        mock.side_effect = [_sim(6), _sim(6), _sim(8)]
        result = await search_profitable_size(None, {}, None, bot_cfg, "base_to_solana")
    assert result is not None
    assert mock.call_count <= 3
