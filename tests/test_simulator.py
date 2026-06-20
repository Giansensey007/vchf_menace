from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.quotes.types import ProviderQuote
from src.scanner.routes import RouteSpec
from src.scanner.simulator import (
    _simulate_fixed_size_vnx_route,
    simulate_cctp_usdc_return_to_vnx,
    simulate_round_trip,
)
from src.treasury.loops import return_leg_direction, use_cctp_usdc_return


@pytest.fixture
def bot_cfg():
    return BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[40],
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
    )


@pytest.fixture
def token():
    return TokenConfig(
        symbol="VCHF",
        decimals=18,
        chains={"vnx": "VCHF", "solana": "mint"},
        chain_decimals={"solana": 9},
    )


@pytest.fixture
def chains():
    return {
        "vnx": ChainConfig(
            key="vnx",
            name="VNX",
            chain_type="vnx",
            chain_id=0,
            enabled=True,
            bridge_verified=True,
            hub_stable="USDC",
            hub_token="USDC",
            hub_decimals=6,
            quote_tier="vnx",
            rpc_env="VNX_API_PUBLIC_KEY",
        ),
        "solana": ChainConfig(
            key="solana",
            name="Sol",
            chain_type="solana",
            chain_id=0,
            enabled=True,
            bridge_verified=True,
            hub_stable="USDC",
            hub_token="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            hub_decimals=6,
            quote_tier="jupiter",
            rpc_env="RPC_SOLANA",
        ),
    }


def _quote(provider: str, amount_in: int, amount_out: int) -> MagicMock:
    q = MagicMock()
    q.provider = provider
    q.amount_in = amount_in
    q.amount_out = amount_out
    return q


@pytest.mark.asyncio
async def test_fixed_size_vnx_route_captures_spread(bot_cfg, chains, token):
    route = RouteSpec("vnx", "solana")

    async def fake_cost(*_a, **_k):
        return 266.0, 200.0, _quote("vnx", 266_000_000, 200 * 10**18)

    with patch(
        "src.scanner.simulator._stable_cost_to_buy_vchf",
        side_effect=fake_cost,
    ), patch(
        "src.scanner.simulator.sell_token_for_stable",
        new_callable=AsyncMock,
        return_value=_quote("jupiter", 200 * 10**9, 268_000_000),
    ):
        sim = await _simulate_fixed_size_vnx_route(
            AsyncMock(), chains, token, bot_cfg, route, 200.0
        )

    assert sim.stable_in_usd == 266.0
    assert sim.stable_out_usd == 268.0
    assert sim.net_profit_usd == pytest.approx(268.0 - 266.0 - sim.fees_usd, rel=1e-3)
    assert sim.net_profit_usd > 0


def test_cctp_return_leg_selected_for_vnx_to_sol():
    assert use_cctp_usdc_return("vnx", "vnx_to_solana", enable_cctp=True)
    assert return_leg_direction("vnx", "vnx_to_solana", enable_cctp=True) == "cctp_sol_usdc_to_vnx"
    assert not use_cctp_usdc_return("solana", "solana_to_vnx", enable_cctp=True)


@pytest.mark.asyncio
async def test_cctp_return_sim(bot_cfg, chains, token):
    cctp_q = MagicMock(ok=True, amount_out_usdc=260.0, fee_usd=1.5, error=None)

    with patch(
        "src.bridge.cctp.CircleCctpBridge.quote_usdc",
        new_callable=AsyncMock,
        return_value=cctp_q,
    ), patch(
        "src.scanner.simulator.buy_token_with_stable",
        new_callable=AsyncMock,
        return_value=_quote("vnx", 260_000_000, int(198 * 10**18)),
    ), patch(
        "src.quotes.vnx._load_quotes",
        new_callable=AsyncMock,
        return_value={"VCHF/USDC": {"b": [1.32, 1000], "a": [1.33, 1000]}},
    ):
        sim = await simulate_cctp_usdc_return_to_vnx(
            AsyncMock(), chains, token, bot_cfg, usdc_on_sol=261.5, target_vchf=200.0
        )

    assert sim.direction == "cctp_sol_usdc_to_vnx"
    assert sim.token_mid == pytest.approx(198.0, rel=1e-2)
    assert sim.stable_in_usd == 261.5


@pytest.mark.asyncio
async def test_round_trip_vnx_uses_cctp_return(bot_cfg, chains, token):
    primary = MagicMock(
        error=None,
        profitable=True,
        sanity_ok=True,
        net_profit_usd=0.5,
        stable_out_usd=267.0,
        token_mid=200.0,
    )
    cctp_ret = MagicMock(
        error=None,
        profitable=False,
        sanity_ok=True,
        net_profit_usd=-2.0,
    )

    with patch(
        "src.scanner.simulator.simulate_direction",
        new_callable=AsyncMock,
        return_value=primary,
    ), patch(
        "src.scanner.simulator.simulate_cctp_usdc_return_to_vnx",
        new_callable=AsyncMock,
        return_value=cctp_ret,
    ) as mock_cctp:
        rt = await simulate_round_trip(
            AsyncMock(), chains, token, bot_cfg, "vnx_to_solana", 200.0, origin="vnx"
        )

    mock_cctp.assert_awaited_once()
    assert rt.return_direction == "cctp_sol_usdc_to_vnx"
    assert rt.round_trip_profit_usd == pytest.approx(-1.5)
