import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig
from src.vnx.bridge import VnxBridge


@pytest.fixture
def bot_cfg():
    return BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        max_trade_vchf=100,
        min_trade_vchf=10,
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
    )


@pytest.mark.asyncio
async def test_bridge_dry_run(bot_cfg):
    os.environ["DRY_RUN"] = "true"
    bridge = VnxBridge(bot_cfg)

    async def fake_deposit(addr):
        return "0xdep"

    with patch("src.vnx.bridge.VnxClient") as mock_cls:
        inst = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = inst
        inst.deposit_address.return_value = {"address": "0xdep123"}
        result = await bridge.bridge_vchf(
            direction="base_to_solana",
            quantity=50.0,
            source_blockchain="BASE",
            dest_blockchain="SOL",
            dest_label="sol-hot",
            deposit_tx_builder=fake_deposit,
        )
    assert result.success
    assert result.dry_run


@pytest.mark.asyncio
async def test_bridge_withdraw_only_dry_run(bot_cfg):
    os.environ["DRY_RUN"] = "true"
    bridge = VnxBridge(bot_cfg)

    with patch("src.vnx.bridge.VnxClient") as mock_cls:
        inst = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = inst
        result = await bridge.bridge_vchf(
            direction="vnx_to_solana",
            quantity=50.0,
            source_blockchain="SOL",
            dest_blockchain="SOL",
            dest_label="sol-hot",
            deposit_tx_builder=lambda _addr: None,
            withdraw_only=True,
        )
    assert result.success
    assert result.dry_run
    inst.deposit_address.assert_not_awaited()
    inst.withdraw.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridge_deposit_only_skips_withdraw(bot_cfg, tmp_path):
    os.environ["DRY_RUN"] = "false"

    async def fake_deposit(addr):
        return "0xdep"

    bridge = VnxBridge(bot_cfg)
    from src.treasury.in_flight import InFlightLedger

    bridge._ledger = InFlightLedger("VCHF", tmp_path / "in_flight.jsonl")

    with patch("src.vnx.bridge.VnxClient") as mock_cls:
        inst = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = inst
        inst.deposit_address.return_value = {"address": "0xdep123"}
        inst.vchf_balance = MagicMock(side_effect=[0.0, 50.0])
        result = await bridge.bridge_vchf(
            direction="solana_to_vnx",
            quantity=50.0,
            source_blockchain="SOL",
            dest_blockchain="SOL",
            dest_label="platform",
            deposit_tx_builder=fake_deposit,
            deposit_only=True,
        )
    assert result.success
    inst.withdraw.assert_not_awaited()
