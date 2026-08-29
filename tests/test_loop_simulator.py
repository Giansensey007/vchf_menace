"""Loop simulator: USD-flow accounting, floors, and buy-back gating."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import BotConfig, ChainConfig, TokenConfig, load_tokens
from src.scanner.loop_simulator import simulate_loop
from src.scanner.routes import (
    LOOP1_OUTBOUND,
    LOOP2_INBOUND,
    LOOP3_CROSS,
    LoopSpec,
    catalog_loops,
)

TOKEN = TokenConfig(
    symbol="VCHF",
    decimals=18,
    chain_decimals={"solana": 9},
    chains={"celo": "0xc", "base": "0xb", "solana": "solV", "vnx": "VCHF", "ethereum": "0xe"},
)


def _chain(key: str, tier: str, *, vnx: bool = False) -> ChainConfig:
    kwargs = dict(
        key=key, name=key.title(), chain_id=0 if vnx else 1, enabled=True,
        bridge_verified=True, quote_tier=tier, hub_stable="USDC", hub_token="USDC",
        hub_decimals=6, rpc_env="RPC",
    )
    if vnx:
        kwargs["chain_type"] = "vnx"
    return ChainConfig(**kwargs)


CHAINS = {
    "celo": _chain("celo", "onchain"),
    "base": _chain("base", "aggregator"),
    "solana": _chain("solana", "jupiter"),
    "ethereum": _chain("ethereum", "aggregator"),
    "vnx": _chain("vnx", "vnx", vnx=True),
}


def _cfg() -> BotConfig:
    return BotConfig(
        poll_interval_sec=60, min_profit_usd=5, min_trade_vchf=30, max_trade_vchf=2000,
        sizing_coarse_step=100, max_sizing_quotes=5, probe_sizes=[40], slippage_bps=50,
        quote_freshness_sec=30, peg_min=0.98, peg_max=1.02, vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600, celo_gas_usd_estimate=0.25, base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05, vnx_bridge_fee_usd=1.0, vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5, enable_vnx_arb_routes=True, enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0, eth_gas_usd_estimate=2.0, platform_vchf_only=True,
        treasury_vchf_home="platform", jit_withdraw=True,
    )


def _mocks(sell_px, buy_px):
    async def fake_sell(client, chain, token, chain_key, amount_in):
        dec = token.chain_decimals.get(chain_key, token.decimals)
        size = amount_in / 10**dec
        return SimpleNamespace(amount_out=int(size * sell_px[chain_key] * 10**chain.hub_decimals), provider="sell")

    async def fake_buy(client, chain, token, chain_key, stable_amount, *, is_buyback=False):
        assert is_buyback, "buy-back legs must pass is_buyback=True"
        usd = stable_amount / 10**chain.hub_decimals
        dec = token.chain_decimals.get(chain_key, token.decimals)
        return SimpleNamespace(amount_out=int((usd / buy_px[chain_key]) * 10**dec), provider="buy")

    return fake_sell, fake_buy


async def _run(loop, *, sell_px, buy_px, ref_bid, size=100.0):
    fake_sell, fake_buy = _mocks(sell_px, buy_px)
    with (
        patch("src.scanner.loop_simulator.sell_token_for_stable", new=fake_sell),
        patch("src.scanner.loop_simulator.buy_token_with_stable", new=fake_buy),
        patch("src.scanner.loop_simulator._platform_ref_price", new=AsyncMock(return_value=(ref_bid, ref_bid))),
        patch("src.scanner.loop_simulator._bridge_fee_usd", new=AsyncMock(return_value=0.3)),
        patch("src.scanner.loop_simulator.min_deposit_usdc", return_value=20.0),
    ):
        return await simulate_loop(AsyncMock(), CHAINS, TOKEN, _cfg(), loop, size)


@pytest.mark.asyncio
async def test_loop1_celo_profitable():
    sim = await _run(LoopSpec(LOOP1_OUTBOUND, "VCHF", "celo"), sell_px={"celo": 1.45}, buy_px={"vnx": 1.30}, ref_bid=1.30)
    assert sim.error is None and sim.profitable and sim.token_out > sim.size


@pytest.mark.asyncio
async def test_loop1_floor_fail():
    sim = await _run(LoopSpec(LOOP1_OUTBOUND, "VCHF", "celo"), sell_px={"celo": 0.40}, buy_px={"vnx": 1.30}, ref_bid=1.30, size=30.0)
    assert not sim.floors_ok and "deposit min" in (sim.error or "")


@pytest.mark.asyncio
async def test_size_below_min_order():
    sim = await _run(LoopSpec(LOOP1_OUTBOUND, "VCHF", "celo"), sell_px={"celo": 1.45}, buy_px={"vnx": 1.30}, ref_bid=1.30, size=10.0)
    assert not sim.floors_ok and "min order" in (sim.error or "")


@pytest.mark.asyncio
async def test_loop2_base_profitable():
    sim = await _run(LoopSpec(LOOP2_INBOUND, "VCHF", "base"), sell_px={"vnx": 1.45}, buy_px={"base": 1.30}, ref_bid=1.45)
    assert sim.error is None and sim.profitable and sim.token_out > sim.size


@pytest.mark.asyncio
async def test_loop3_base_to_solana_profitable():
    sim = await _run(LoopSpec(LOOP3_CROSS, "VCHF", "base", "solana"), sell_px={"base": 1.45}, buy_px={"solana": 1.30}, ref_bid=1.40)
    assert sim.error is None and sim.profitable
    assert len([leg for leg in sim.legs if leg.kind == "bridge_stable"]) == 1


def _live_loops():
    return catalog_loops(load_tokens()["VCHF"])


def _px_book(sell: float, buy: float) -> tuple[dict[str, float], dict[str, float]]:
    venues = ("celo", "base", "solana", "ethereum", "vnx")
    return {k: sell for k in venues}, {k: buy for k in venues}


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", _live_loops(), ids=lambda loop: loop.key)
async def test_simulate_loop_every_live_key(loop):
    sell_px, buy_px = _px_book(1.45, 1.30)
    sim = await _run(loop, sell_px=sell_px, buy_px=buy_px, ref_bid=1.32, size=100.0)
    assert sim.error is None
    assert sim.floors_ok
    kinds = [leg.kind for leg in sim.legs]
    if loop.family == LOOP1_OUTBOUND:
        if loop.chain_a == loop.hub:
            assert kinds == ["sell_onchain", "vnx_usdc_deposit", "platform_buyback"]
        else:
            assert kinds == ["sell_onchain", "bridge_stable", "vnx_usdc_deposit", "platform_buyback"]
    elif loop.family == LOOP2_INBOUND:
        if loop.chain_a == loop.hub:
            assert kinds == ["platform_sell", "onchain_buyback", "vnx_token_deposit"]
        else:
            assert kinds == ["platform_sell", "bridge_stable", "onchain_buyback", "vnx_token_deposit"]
    else:
        assert kinds == ["sell_onchain", "bridge_stable", "onchain_buyback", "vnx_token_deposit"]
        mech = loop.bridge_legs[0].mechanism
        assert mech in sim.legs[1].detail
        pair = {loop.chain_a, loop.chain_b}
        if pair == {"celo", "base"}:
            assert mech == "eth_triangle"
        if pair == {"base", "solana"}:
            assert mech == "cctp"
