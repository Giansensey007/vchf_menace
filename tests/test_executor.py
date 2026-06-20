import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig, load_chains, load_tokens
from src.execution.executor import ArbExecutor, CycleRecord, CycleState
from src.scanner.simulator import CycleSimulation


def _bot_cfg(**overrides) -> BotConfig:
    base = dict(
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
    base.update(overrides)
    return BotConfig(**base)


def _profitable_sim(direction: str, **overrides) -> CycleSimulation:
    parts = direction.split("_to_")
    base = dict(
        direction=direction,
        buy_chain=parts[0],
        sell_chain=parts[1],
        size_vchf=500,
        stable_in_usd=700,
        stable_out_usd=720,
        token_mid=498,
        net_profit_usd=15,
        profitable=True,
    )
    base.update(overrides)
    return CycleSimulation(**base)


def test_cycle_record_failed():
    r = CycleRecord(id="abc", direction="celo_to_solana", size_vchf=10)
    r.state = CycleState.FAILED
    r.error = "not profitable"
    assert r.state == CycleState.FAILED


@pytest.mark.asyncio
async def test_run_cycle_blocks_disabled_cctp():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg(enable_vnx_cctp_routes=False)
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    sim = _profitable_sim("solana_to_vnx")

    with patch("src.execution.executor.simulate_direction", new_callable=AsyncMock, return_value=sim):
        with patch("src.execution.executor.save_cycle"):
            with patch("src.execution.executor.log_cycle_step"):
                rec = await ex.run_cycle(AsyncMock(), "solana_to_vnx", 500)

    assert rec.state == CycleState.FAILED
    assert "CCTP" in (rec.error or "")


@pytest.mark.asyncio
async def test_run_cycle_blocks_disabled_vnx_arb():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg(enable_vnx_arb_routes=False)
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    sim = _profitable_sim("celo_to_vnx")

    with patch("src.execution.executor.simulate_direction", new_callable=AsyncMock, return_value=sim):
        with patch("src.execution.executor.save_cycle"):
            with patch("src.execution.executor.log_cycle_step"):
                rec = await ex.run_cycle(AsyncMock(), "celo_to_vnx", 500)

    assert rec.state == CycleState.FAILED
    assert "celo↔vnx disabled" in (rec.error or "")


@pytest.mark.asyncio
async def test_cctp_reconcile_directions():
    os.environ["DRY_RUN"] = "true"
    os.environ["CCTP_RECONCILE_USDC"] = "100"
    cfg = _bot_cfg()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    record = CycleRecord(id="t1", direction="solana_to_vnx", size_vchf=500)
    cctp = AsyncMock()
    cctp.bridge_usdc_eth_to_sol = AsyncMock(
        return_value=MagicMock(direction="ethereum_to_solana_usdc", success=True, dry_run=True)
    )
    cctp.bridge_usdc_sol_to_eth = AsyncMock(
        return_value=MagicMock(direction="solana_to_ethereum_usdc", success=True, dry_run=True)
    )

    with patch("src.execution.executor.CircleCctpBridge", return_value=cctp):
        with patch("src.execution.executor.log_cycle_step"):
            await ex._reconcile_cctp_platform(AsyncMock(), record, "solana_to_vnx", 100.0)
            cctp.bridge_usdc_eth_to_sol.assert_awaited_once()
            cctp.bridge_usdc_sol_to_eth.assert_not_awaited()

            cctp.reset_mock()
            await ex._reconcile_cctp_platform(AsyncMock(), record, "vnx_to_solana", 100.0)
            cctp.bridge_usdc_sol_to_eth.assert_awaited_once()
            cctp.bridge_usdc_eth_to_sol.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_to_vnx_deposits_then_platform_sells():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    record = CycleRecord(id="t5", direction="solana_to_vnx", size_vchf=500)
    sim = _profitable_sim("solana_to_vnx")

    mock_sol = MagicMock()
    mock_sol.swap = AsyncMock(return_value="dry-run-sol-buy")
    mock_sol.token_balance_ui = MagicMock(return_value=500.0)
    mock_bridge = AsyncMock()
    mock_bridge.bridge_vchf = AsyncMock(
        return_value=MagicMock(success=True, dry_run=True, deposit_tx="dry-run-dep")
    )
    mock_sell = AsyncMock(
        return_value=MagicMock(
            success=True, dry_run=True, quantity=498, price=1.35, ordid=0, ordstatus="Filled"
        )
    )

    with patch("src.execution.executor.platform_sell_vchf", new=mock_sell):
        with patch("src.execution.executor.VnxBridge", return_value=mock_bridge):
            with patch("src.execution.executor.SolanaExecutor", return_value=mock_sol):
                with patch("src.execution.executor.log_cycle_step"):
                    with patch("src.execution.executor.route_for_direction"):
                        await ex._exec_chain_to_vnx(AsyncMock(), record, sim, "solana")

    mock_bridge.bridge_vchf.assert_awaited_once()
    assert mock_bridge.bridge_vchf.call_args.kwargs["deposit_only"] is True
    mock_sell.assert_awaited_once()
    assert record.state == CycleState.DONE


@pytest.mark.asyncio
async def test_chain_to_vnx_fails_on_swap():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    record = CycleRecord(id="t2", direction="solana_to_vnx", size_vchf=500)
    sim = _profitable_sim("solana_to_vnx")

    mock_sol = MagicMock()
    mock_sol.swap = AsyncMock(return_value=None)

    with patch("src.execution.executor.SolanaExecutor", return_value=mock_sol):
        with patch("src.execution.executor.log_cycle_step"):
            await ex._exec_chain_to_vnx(AsyncMock(), record, sim, "solana")

    assert record.state == CycleState.FAILED
    assert "solana buy VCHF failed" in (record.error or "")


@pytest.mark.asyncio
async def test_vnx_to_chain_uses_withdraw_only():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    record = CycleRecord(id="t3", direction="vnx_to_solana", size_vchf=500)
    sim = _profitable_sim("vnx_to_solana")

    mock_bridge = AsyncMock()
    mock_bridge.bridge_vchf = AsyncMock(
        return_value=MagicMock(
            success=True,
            dry_run=True,
            deposit_tx=None,
            withdraw_txids=["dry-run-withdraw"],
            quantity=498,
        )
    )
    mock_sol = MagicMock()
    mock_sol.swap = AsyncMock(return_value="dry-run-solana-tx")
    bal_resp = MagicMock()
    bal_resp.value.amount = "498000000000"
    mock_sol.token_account_balance.return_value = bal_resp
    mock_buy = AsyncMock(
        return_value=MagicMock(
            success=True, dry_run=True, quantity=498, price=1.36, ordid=0, ordstatus="Filled"
        )
    )

    with patch("src.execution.executor.platform_buy_vchf", new=mock_buy):
        with patch("src.execution.executor.VnxBridge", return_value=mock_bridge):
            with patch("src.execution.executor.SolanaExecutor", return_value=mock_sol):
                with patch("src.execution.executor.VnxClient") as mock_vnx_cls:
                    vnx_inst = AsyncMock()
                    mock_vnx_cls.return_value.__aenter__.return_value = vnx_inst
                    vnx_inst.account_balance = AsyncMock(return_value={"balances": []})
                    vnx_inst.vchf_balance = MagicMock(return_value=0.0)
                    with patch("src.execution.executor.log_cycle_step"):
                        with patch("src.execution.executor.route_for_direction"):
                            await ex._exec_vnx_to_chain(AsyncMock(), record, sim, "solana")

    mock_buy.assert_awaited_once()
    mock_bridge.bridge_vchf.assert_awaited_once()
    kwargs = mock_bridge.bridge_vchf.call_args.kwargs
    assert kwargs["withdraw_only"] is True
    assert kwargs["quantity"] == sim.token_mid
    assert record.state == CycleState.DONE


@pytest.mark.asyncio
async def test_vnx_to_chain_fails_on_sell():
    os.environ["DRY_RUN"] = "true"
    cfg = _bot_cfg()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ex = ArbExecutor(chains, token, cfg)
    record = CycleRecord(id="t4", direction="vnx_to_celo", size_vchf=500)
    sim = _profitable_sim("vnx_to_celo", buy_chain="vnx", sell_chain="celo")

    mock_bridge = AsyncMock()
    mock_bridge.bridge_vchf = AsyncMock(
        return_value=MagicMock(success=True, dry_run=True, quantity=498)
    )
    mock_celo = MagicMock()
    mock_celo.balance_erc20 = MagicMock(return_value=10**18 * 500)
    mock_celo.swap_exact_input = MagicMock(return_value=None)
    mock_buy = AsyncMock(
        return_value=MagicMock(
            success=True, dry_run=True, quantity=498, price=1.36, ordid=0, ordstatus="Filled"
        )
    )

    with patch("src.execution.executor.platform_buy_vchf", new=mock_buy):
        with patch("src.execution.executor.VnxBridge", return_value=mock_bridge):
            with patch("src.execution.executor.BaseExecutor", return_value=mock_celo):
                with patch("src.execution.executor.VnxClient") as mock_vnx_cls:
                    vnx_inst = AsyncMock()
                    mock_vnx_cls.return_value.__aenter__.return_value = vnx_inst
                    vnx_inst.account_balance = AsyncMock(return_value={"balances": []})
                    vnx_inst.vchf_balance = MagicMock(return_value=0.0)
                    with patch("src.execution.executor.log_cycle_step"):
                        await ex._exec_vnx_to_chain(AsyncMock(), record, sim, "celo")

    assert record.state == CycleState.FAILED
    assert "celo sell VCHF failed" in (record.error or "")
