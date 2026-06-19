import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig
from src.vnx.trading import platform_buy_vchf, platform_sell_vchf


def _cfg(**overrides) -> BotConfig:
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
        celo_gas_usd_estimate=0.25,
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
    base.update(overrides)
    return BotConfig(**base)


@pytest.fixture
def mock_vnx():
    vnx = AsyncMock()
    vnx.get_trading_pairs.return_value = {
        "pairs": [{"pair": "VCHF/USDC", "qty_decimals": 4, "status": "online"}]
    }
    vnx.get_quotes.return_value = {
        "quotes": [{"symbol": "VCHF/USDC", "b": [1.35, 5000], "a": [1.36, 5000]}]
    }
    vnx.account_balance.return_value = {
        "balances": [
            {"asset": "VCHF", "available_balance": 1000},
            {"asset": "USDC", "available_balance": 10000},
        ]
    }
    vnx.vchf_balance = MagicMock(return_value=1000.0)
    vnx.usdc_balance = MagicMock(return_value=10000.0)
    vnx.add_order.return_value = {
        "result": "success",
        "order": {
            "clordid": "sell-abc",
            "ordid": 42,
            "ordstatus": "Filled",
            "bought": 675,
            "bought_currency": "USDC",
            "sold": 500,
            "sold_currency": "VCHF",
            "fee": 1.0,
            "fee_currency": "USDC",
        },
    }
    return vnx


@pytest.mark.asyncio
async def test_platform_sell_dry_run(mock_vnx):
    os.environ["DRY_RUN"] = "true"
    result = await platform_sell_vchf(_cfg(), 500.0, vnx=mock_vnx)
    assert result.success
    assert result.dry_run
    assert result.side == "Sell"
    mock_vnx.add_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_platform_sell_live_fok(mock_vnx):
    os.environ["DRY_RUN"] = "false"
    result = await platform_sell_vchf(_cfg(), 500.0, vnx=mock_vnx)
    assert result.success
    assert result.ordstatus == "Filled"
    mock_vnx.add_order.assert_awaited_once()
    payload = mock_vnx.add_order.call_args.args[0]
    assert payload["side"] == "Sell"
    assert payload["symbol"] == "VCHF/USDC"
    assert payload["timeinforce"] == "FOK"
    assert payload["orderqty"] == 500.0


@pytest.mark.asyncio
async def test_platform_buy_rejects_below_min(mock_vnx):
    os.environ["DRY_RUN"] = "true"
    result = await platform_buy_vchf(_cfg(), 10.0, vnx=mock_vnx)
    assert not result.success
    assert "min order" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_platform_buy_insufficient_usdc(mock_vnx):
    os.environ["DRY_RUN"] = "false"
    mock_vnx.account_balance.return_value = {
        "balances": [{"asset": "USDC", "available_balance": 1}]
    }
    mock_vnx.usdc_balance = MagicMock(return_value=1.0)
    result = await platform_buy_vchf(_cfg(), 500.0, vnx=mock_vnx)
    assert not result.success
    assert "insufficient platform USDC" in (result.error or "")


@pytest.mark.asyncio
async def test_platform_sell_order_rejected(mock_vnx):
    os.environ["DRY_RUN"] = "false"
    mock_vnx.add_order.return_value = {
        "result": "error",
        "error": {"code": "non_marketable_fok_limit_price", "message": "FOK killed"},
    }
    result = await platform_sell_vchf(_cfg(), 500.0, vnx=mock_vnx)
    assert not result.success
    assert "FOK" in (result.error or "")
