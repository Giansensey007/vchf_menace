"""Stress tests for choose_execution, search_profitable_size, active_routes."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import BotConfig
from src.scanner.routes import active_directions, active_routes, route_for_direction
from src.scanner.selector import RouteGroupBest, choose_execution
from src.scanner.sizing import search_profitable_size
from src.scanner.simulator import CycleSimulation


def _cfg(**overrides) -> BotConfig:
    base = dict(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[50],
        slippage_bps=50,
        quote_freshness_sec=30,
        peg_min=0.98,
        peg_max=1.02,
        vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600,
        celo_gas_usd_estimate=0.25,
        base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0,
        vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=True,
        enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0,
        cctp_fee_usd=1.5,
        close_loop_after_cycle=False,
    )
    base.update(overrides)
    return BotConfig(**base)


def _sim(direction: str, net: float, *, sanity_ok: bool = True, error: str | None = None) -> CycleSimulation:
    parts = direction.split("_to_")
    return CycleSimulation(
        direction=direction,
        buy_chain=parts[0],
        sell_chain=parts[1],
        size_vchf=500,
        stable_in_usd=700,
        stable_out_usd=700 + net,
        token_mid=500,
        net_profit_usd=net,
        profitable=net > 0,
        sanity_ok=sanity_ok,
        error=error,
    )


def _best(group: str, direction: str, net: float, **kw) -> RouteGroupBest:
    return RouteGroupBest(group, direction, 500, net, _sim(direction, net, **kw))


class TestChooseExecution:
    def test_neither_profitable(self):
        cfg = _cfg()
        r = choose_execution(None, None, cfg)
        assert r.opportunity is None
        assert "no profitable" in r.reason

    def test_only_vnx_sol(self):
        cfg = _cfg()
        vs = _best("vnx_sol", "vnx_to_solana", 8)
        r = choose_execution(None, vs, cfg)
        assert r.opportunity is vs

    def test_exact_premium_boundary_prefers_indirect(self):
        cfg = _cfg(indirect_route_premium_usd=5.0)
        cs = _best("base_sol", "base_to_solana", 10)
        vs = _best("vnx_sol", "vnx_to_solana", 15)  # delta == 5
        r = choose_execution(cs, vs, cfg)
        assert r.opportunity is vs

    def test_one_below_premium_prefers_celo(self):
        cfg = _cfg(indirect_route_premium_usd=5.0)
        cs = _best("base_sol", "base_to_solana", 10)
        vs = _best("vnx_sol", "vnx_to_solana", 14.99)
        r = choose_execution(cs, vs, cfg)
        assert r.opportunity is cs

    def test_negative_delta_still_picks_celo(self):
        cfg = _cfg()
        cs = _best("base_sol", "solana_to_base", 20)
        vs = _best("vnx_sol", "solana_to_vnx", 10)
        r = choose_execution(cs, vs, cfg)
        assert r.opportunity is cs

    def test_zero_premium_picks_higher_profit(self):
        cfg = _cfg(indirect_route_premium_usd=0.0)
        cs = _best("base_sol", "base_to_solana", 10)
        vs = _best("vnx_sol", "vnx_to_solana", 10.01)
        r = choose_execution(cs, vs, cfg)
        assert r.opportunity is vs

    def test_base_vnx_wins_over_base_sol(self):
        cfg = _cfg()
        cs = _best("base_sol", "base_to_solana", 10)
        cv = _best("base_vnx", "base_to_vnx", 25)
        r = choose_execution(cs, None, cfg, base_vnx=cv)
        assert r.opportunity is cv

    def test_base_vnx_loses_to_better_base_sol(self):
        cfg = _cfg()
        cs = _best("base_sol", "base_to_solana", 30)
        cv = _best("base_vnx", "vnx_to_base", 12)
        r = choose_execution(cs, None, cfg, base_vnx=cv)
        assert r.opportunity is cs


class TestActiveRoutes:
    def test_default_ten_routes(self):
        cfg = _cfg()
        routes = active_routes(cfg)
        assert len(routes) == 10
        dirs = {r.direction for r in routes}
        assert dirs == {
            "celo_to_solana",
            "solana_to_celo",
            "celo_to_vnx",
            "vnx_to_celo",
            "base_to_solana",
            "solana_to_base",
            "base_to_vnx",
            "vnx_to_base",
            "solana_to_vnx",
            "vnx_to_solana",
        }

    def test_arb_disabled_drops_evm_vnx(self):
        cfg = _cfg(enable_vnx_arb_routes=False)
        assert len(active_routes(cfg)) == 6
        assert "base_to_vnx" not in active_directions(cfg)
        assert "celo_to_vnx" not in active_directions(cfg)

    def test_cctp_disabled(self):
        cfg = _cfg(enable_vnx_cctp_routes=False)
        assert len(active_routes(cfg)) == 8
        assert set(active_directions(cfg)) == {
            "celo_to_solana",
            "solana_to_celo",
            "celo_to_vnx",
            "vnx_to_celo",
            "base_to_solana",
            "solana_to_base",
            "base_to_vnx",
            "vnx_to_base",
        }

    def test_all_routes_when_both_enabled(self, monkeypatch):
        monkeypatch.setenv("ENABLE_VNX_ARB_ROUTES", "true")
        monkeypatch.setenv("ENABLE_VNX_CCTP_ROUTES", "true")
        from src.config_loader import load_bot_config

        cfg = load_bot_config()
        assert len(active_routes(cfg)) == 10

    def test_all_directions_have_route_spec(self):
        for d in active_directions(_cfg()):
            assert route_for_direction(d) is not None


def _sim_result(net: float, error: str | None = None, sanity_ok: bool = True):
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
        sanity_ok=sanity_ok,
    )


@pytest.mark.asyncio
async def test_search_invalid_range():
    cfg = _cfg(min_trade_vchf=2000, max_trade_vchf=200)
    result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is None


@pytest.mark.asyncio
async def test_search_finds_interior_sweet_spot():
    """Endpoints can be unprofitable while a mid-range size is profitable (slippage curve)."""
    cfg = _cfg(min_trade_vchf=200, max_trade_vchf=2000, sizing_coarse_step=100)

    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        if abs(size - 1100) < 1:
            return _sim_result(12)
        return _sim_result(2)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is not None
    assert result.simulation.net_profit_usd == 12


@pytest.mark.asyncio
async def test_search_equal_min_max():
    cfg = _cfg(min_trade_vchf=500, max_trade_vchf=500)
    with patch("src.scanner.sizing.simulate_direction", new_callable=AsyncMock) as mock:
        mock.return_value = _sim_result(10)
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is not None
    assert result.size_vchf == 500
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_search_prefers_larger_size_on_tie():
    cfg = _cfg(min_trade_vchf=200, max_trade_vchf=2000, sizing_coarse_step=100)

    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        return _sim_result(10 if size >= 200 else -1)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is not None
    assert result.size_vchf == 2000


@pytest.mark.asyncio
async def test_search_error_at_endpoint_still_searches():
    cfg = _cfg(min_trade_vchf=200, max_trade_vchf=2000, sizing_coarse_step=100)

    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        if size <= 200:
            return _sim_result(-1, error="quote fail")
        return _sim_result(8)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is not None
    assert result.size_vchf >= 200


@pytest.mark.asyncio
async def test_search_rejects_failed_sanity():
    cfg = _cfg(min_profit_usd=5)

    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        return _sim_result(20, sanity_ok=False)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is None


@pytest.mark.asyncio
async def test_search_below_min_profit_not_returned():
    cfg = _cfg(min_profit_usd=5)

    async def fake_sim(_c, _ch, _t, _cfg, _dir, size, **_kw):
        return _sim_result(3)

    with patch("src.scanner.sizing.simulate_direction", side_effect=fake_sim):
        result = await search_profitable_size(None, {}, None, cfg, "base_to_solana")
    assert result is None
