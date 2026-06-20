"""Platform-only treasury: on-chain token buy paths must be blocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.platform_policy import (
    on_chain_buy_blocked_message,
    on_chain_token_buy_blocked,
    platform_buy_opener_blocked,
    platform_buy_opener_blocked_message,
    platform_token_only,
    route_requires_on_chain_token_buy,
)
from src.quotes.router import buy_token_with_stable
from src.scanner.routes import RouteSpec, active_routes


def _cfg(*, platform_only: bool = True) -> BotConfig:
    return BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=30,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[40],
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
        platform_vchf_only=platform_only,
        treasury_vchf_home="platform",
        jit_withdraw=True,
    )


def test_platform_token_only_defaults_true():
    assert platform_token_only(_cfg())
    assert not platform_token_only(_cfg(platform_only=False))


def test_on_chain_buy_blocked_off_vnx_only():
    cfg = _cfg()
    assert on_chain_token_buy_blocked(cfg, "celo")
    assert on_chain_token_buy_blocked(cfg, "base")
    assert not on_chain_token_buy_blocked(cfg, "vnx")


def test_active_routes_skip_on_chain_buy_when_platform_only():
    cfg = _cfg()
    directions = {r.direction for r in active_routes(cfg)}
    assert "vnx_to_base" in directions
    assert "base_to_vnx" not in directions
    assert "celo_to_solana" not in directions


@pytest.mark.asyncio
async def test_router_buy_token_with_stable_returns_none_on_base():
    chain = ChainConfig(
        key="base",
        name="Base",
        chain_id=8453,
        enabled=True,
        bridge_verified=True,
        quote_tier="aggregator",
        hub_stable="USDC",
        hub_token="0xusdc",
        hub_decimals=6,
        rpc_env="RPC_BASE",
    )
    token = TokenConfig(symbol="VCHF", decimals=18, chains={"base": "0xvchf"})
    with patch("src.quotes.router.load_bot_config", return_value=_cfg()):
        q = await buy_token_with_stable(AsyncMock(), chain, token, "base", 1_000_000)
    assert q is None


def test_route_requires_on_chain_token_buy():
    assert route_requires_on_chain_token_buy(RouteSpec("base", "solana"))
    assert not route_requires_on_chain_token_buy(RouteSpec("vnx", "base"))


def test_blocked_message_mentions_platform_flag():
    assert "platform_vchf_only" in on_chain_buy_blocked_message(_cfg(), "celo")


def test_on_chain_buyback_leg_allowed_under_platform_only():
    cfg = _cfg()
    assert on_chain_token_buy_blocked(cfg, "celo")
    assert on_chain_token_buy_blocked(cfg, "base")
    assert not on_chain_token_buy_blocked(cfg, "celo", is_buyback=True)
    assert not on_chain_token_buy_blocked(cfg, "base", is_buyback=True)
    assert not on_chain_token_buy_blocked(cfg, "vnx")


def test_platform_buy_opener_blocked_except_buyback():
    cfg = _cfg()
    assert platform_buy_opener_blocked(cfg)
    assert not platform_buy_opener_blocked(cfg, is_buyback=True)
    assert not platform_buy_opener_blocked(_cfg(platform_only=False))
    assert "platform_vchf_only" in platform_buy_opener_blocked_message(cfg)
