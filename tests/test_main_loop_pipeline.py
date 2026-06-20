"""run_once must route to the loop pipeline only when ENABLE_LOOP_PIPELINE is on (VCHF)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig
from src.main import run_once


def _cfg(*, enable_loop_pipeline: bool) -> BotConfig:
    return BotConfig(
        poll_interval_sec=60, min_profit_usd=5, min_trade_vchf=200, max_trade_vchf=2000,
        sizing_coarse_step=100, max_sizing_quotes=3, probe_sizes=[200], slippage_bps=50,
        quote_freshness_sec=30, peg_min=0.98, peg_max=1.02, vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600, celo_gas_usd_estimate=0.25, base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05, vnx_bridge_fee_usd=1.0, vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5, enable_vnx_arb_routes=True, enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0, eth_gas_usd_estimate=2.0, platform_vchf_only=True,
        treasury_vchf_home="platform", jit_withdraw=True, enable_loop_pipeline=enable_loop_pipeline,
    )


@pytest.mark.asyncio
async def test_run_once_executes_selected_loop_when_pipeline_enabled():
    treasury = MagicMock()
    treasury.snapshot = AsyncMock(return_value=MagicMock())
    treasury.balance_line = MagicMock(return_value="bal")

    best = MagicMock(loop_key="loop1_outbound:base", size=300.0, token="VCHF", net_profit_usd=12.0, fees_usd=1.0)
    selection = MagicMock(best=best, reason="best")
    loop = MagicMock()
    record = MagicMock(
        id="abc", loop_key="loop1_outbound:base", state=MagicMock(value="done"),
        steps_done=["withdraw_token"], tx_hashes=["0x1"], error=None,
    )
    executor = MagicMock()
    run_loop = AsyncMock(return_value=record)
    executor.run_loop = run_loop

    with (
        patch("src.main.load_bot_config", return_value=_cfg(enable_loop_pipeline=True)),
        patch("src.main.load_chains", return_value={}),
        patch("src.main.load_tokens", return_value={"VCHF": MagicMock()}),
        patch("src.main.TreasuryManager", return_value=treasury),
        patch("src.main.build_client") as mock_client,
        patch("src.scanner.loop_selector.select_best_loop", new=AsyncMock(return_value=selection)),
        patch("src.scanner.routes.loop_for_key", new=MagicMock(return_value=loop)),
        patch("src.execution.loop_executor.LoopExecutor", return_value=executor),
    ):
        mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        await run_once()

    run_loop.assert_awaited_once()
    assert run_loop.call_args.args[1] is loop
    assert run_loop.call_args.args[2] == 300.0


@pytest.mark.asyncio
async def test_run_once_skips_loop_pipeline_when_disabled():
    treasury = MagicMock()
    treasury.snapshot = AsyncMock(return_value=MagicMock())
    treasury.balance_line = MagicMock(return_value="bal")
    treasury.consolidate_vchf_to_platform = AsyncMock(return_value=0.0)

    scanner = MagicMock()
    scanner.best_opportunity = AsyncMock(return_value=None)
    scanner.last_selection = None

    select = AsyncMock()
    with (
        patch("src.main.load_bot_config", return_value=_cfg(enable_loop_pipeline=False)),
        patch("src.main.load_chains", return_value={}),
        patch("src.main.load_tokens", return_value={"VCHF": MagicMock()}),
        patch("src.main.TreasuryManager", return_value=treasury),
        patch("src.main.ArbScanner", return_value=scanner),
        patch("src.scanner.loop_selector.select_best_loop", new=select),
    ):
        await run_once()

    scanner.best_opportunity.assert_awaited_once()
    select.assert_not_awaited()
