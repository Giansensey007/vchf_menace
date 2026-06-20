"""LoopExecutor: dry-run step dispatch for Loop 1/2/3 + live gating (VCHF)."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.execution.loop_executor import LoopExecutor, LoopState
from src.scanner.loop_simulator import LoopLeg, LoopSimulation
from src.scanner.routes import LOOP1_OUTBOUND, LOOP2_INBOUND, LOOP3_CROSS, LoopSpec

MOD = "src.execution.loop_executor"

TOKEN = TokenConfig(
    symbol="VCHF",
    decimals=18,
    chain_decimals={"solana": 9},
    chains={"celo": "0xcelo", "base": "0xbase", "solana": "solV", "vnx": "VCHF", "ethereum": "0xeth"},
)


def _chain(key: str, *, vnx: bool = False) -> ChainConfig:
    kwargs = dict(
        key=key, name=key.title(), chain_id=0 if vnx else 1, enabled=True,
        bridge_verified=True, quote_tier="onchain", hub_stable="USDC",
        hub_token="USDC", hub_decimals=6, rpc_env="RPC",
    )
    if vnx:
        kwargs["chain_type"] = "vnx"
    return ChainConfig(**kwargs)


CHAINS = {
    "celo": _chain("celo"),
    "base": _chain("base"),
    "solana": _chain("solana"),
    "ethereum": _chain("ethereum"),
    "vnx": _chain("vnx", vnx=True),
}


def _cfg(*, enable_loop_executor: bool = False) -> BotConfig:
    return BotConfig(
        poll_interval_sec=60, min_profit_usd=5, min_trade_vchf=40, max_trade_vchf=2000,
        sizing_coarse_step=100, max_sizing_quotes=5, probe_sizes=[40], slippage_bps=50,
        quote_freshness_sec=30, peg_min=0.98, peg_max=1.02, vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600, celo_gas_usd_estimate=0.25, base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0, vnx_platform_fee_usd=0.5, wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=True, enable_vnx_cctp_routes=True, indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0, platform_vchf_only=True, treasury_vchf_home="platform",
        jit_withdraw=True, enable_loop_executor=enable_loop_executor,
    )


def _bridge_ok():
    return SimpleNamespace(
        success=True, dry_run=True, source_tx="0xsrc", dest_tx="0xdst", direction="x", error=None,
        amount_usdc=100.0,
    )


def _sim(loop: LoopSpec, *, size: float, token_out: float, legs: list[LoopLeg],
         profitable: bool = True, error: str | None = None) -> LoopSimulation:
    sim = LoopSimulation(loop_key=loop.key, family=loop.family, token="VCHF", size=size)
    sim.token_out = token_out
    sim.net_token = token_out - size
    sim.ref_price = 1.0
    sim.net_profit_usd = sim.net_token * 1.0
    sim.profitable = profitable
    sim.floors_ok = profitable
    sim.legs = legs
    sim.error = error
    return sim


def _evm_mock():
    m = MagicMock()
    m.swap_exact_input.return_value = "0xevmswap"
    m.transfer_erc20.return_value = "0xevmtransfer"
    m.balance_erc20.return_value = 10 ** 40
    m.address = "0xEvmAddr"
    return m


def _apply(stack: ExitStack, sim: LoopSimulation, *, dry_run: bool = True) -> None:
    sol = MagicMock()
    sol.swap = AsyncMock(return_value="solswap")
    sol.transfer_spl.return_value = "soltransfer"

    vnxb = MagicMock()
    vnxb.bridge_vchf = AsyncMock(
        return_value=SimpleNamespace(
            success=True, quantity=None, deposit_tx="0xdep", withdraw_txids=["0xwd"],
            dry_run=True, error=None,
        )
    )
    usdcb = MagicMock()
    usdcb.withdraw_usdc = AsyncMock(
        return_value=SimpleNamespace(success=True, withdraw_txids=["0xu"], dry_run=True, error=None)
    )
    cctp = MagicMock()
    for name in (
        "bridge_usdc_sol_to_eth", "bridge_usdc_eth_to_sol", "bridge_usdc_base_to_eth",
        "bridge_usdc_eth_to_base", "bridge_usdc_base_to_sol", "bridge_usdc_sol_to_base",
    ):
        setattr(cctp, name, AsyncMock(return_value=_bridge_ok()))
    wh = MagicMock()
    for name in (
        "bridge_usdt_celo_to_ethereum", "bridge_usdt_ethereum_to_celo",
        "bridge_usdt_celo_to_solana", "bridge_usdt_solana_to_celo",
    ):
        setattr(wh, name, AsyncMock(return_value=_bridge_ok()))

    stack.enter_context(patch(f"{MOD}.simulate_loop", new=AsyncMock(return_value=sim)))
    stack.enter_context(patch(f"{MOD}.is_dry_run", new=MagicMock(return_value=dry_run)))
    stack.enter_context(patch(f"{MOD}.CeloExecutor", new=MagicMock(return_value=_evm_mock())))
    stack.enter_context(patch(f"{MOD}.BaseExecutor", new=MagicMock(return_value=_evm_mock())))
    stack.enter_context(patch(f"{MOD}.EthereumExecutor", new=MagicMock(return_value=_evm_mock())))
    stack.enter_context(patch(f"{MOD}.SolanaExecutor", new=MagicMock(return_value=sol)))
    stack.enter_context(patch(f"{MOD}.VnxBridge", new=MagicMock(return_value=vnxb)))
    stack.enter_context(patch(f"{MOD}.VnxUsdcBridge", new=MagicMock(return_value=usdcb)))
    stack.enter_context(patch(f"{MOD}.CircleCctpBridge", new=MagicMock(return_value=cctp)))
    stack.enter_context(patch(f"{MOD}.WormholePortalBridge", new=MagicMock(return_value=wh)))
    stack.enter_context(
        patch(f"{MOD}.platform_sell_vchf", new=AsyncMock(
            return_value=SimpleNamespace(success=True, quantity=100, price=1.0, ordid="1", ordstatus="2", dry_run=True, error=None)))
    )
    stack.enter_context(
        patch(f"{MOD}.platform_buy_vchf", new=AsyncMock(
            return_value=SimpleNamespace(success=True, quantity=110, price=1.0, ordid="2", ordstatus="2", dry_run=True, error=None)))
    )
    stack.enter_context(patch(f"{MOD}.eth_usdc_to_vnx", new=AsyncMock(return_value={"success": True, "deposit_tx": "0xeth"})))
    stack.enter_context(patch(f"{MOD}.validate_eth_usdc_vnx_deposit", new=MagicMock(return_value=None)))


async def _run(loop, sim, *, dry_run=True, enable=False, force=False):
    ex = LoopExecutor(CHAINS, TOKEN, _cfg(enable_loop_executor=enable))
    with ExitStack() as stack:
        _apply(stack, sim, dry_run=dry_run)
        return await ex.run_loop(MagicMock(), loop, sim.size, force_execute=force)


def _l1_legs(chain):
    return [
        LoopLeg("sell_onchain", chain, "", 145.0),
        LoopLeg("bridge_stable", chain, "", 143.0),
        LoopLeg("vnx_usdc_deposit", "ethereum", "", 140.0),
        LoopLeg("platform_buyback", "vnx", "", 140.0),
    ]


@pytest.mark.asyncio
async def test_loop1_base_uses_cctp_to_hub():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "base")
    sim = _sim(loop, size=100.0, token_out=110.0, legs=_l1_legs("base"))
    rec = await _run(loop, sim)
    assert rec.state == LoopState.DONE
    assert rec.steps_done == [
        "withdraw_token", "sell_token_onchain", "bridge_base_ethereum",
        "vnx_usdc_deposit", "platform_buyback",
    ]


@pytest.mark.asyncio
async def test_loop1_celo_uses_wormhole_to_hub():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "celo")
    sim = _sim(loop, size=100.0, token_out=108.0, legs=_l1_legs("celo"))
    rec = await _run(loop, sim)
    assert rec.state == LoopState.DONE
    assert "bridge_celo_ethereum" in rec.steps_done


@pytest.mark.asyncio
async def test_loop2_base_runs_all_steps():
    loop = LoopSpec(LOOP2_INBOUND, "VCHF", "base")
    legs = [
        LoopLeg("platform_sell", "vnx", "", 145.0),
        LoopLeg("bridge_stable", "ethereum", "", 143.0),
        LoopLeg("onchain_buyback", "base", "", 143.0),
        LoopLeg("vnx_token_deposit", "base", "", 143.0),
    ]
    sim = _sim(loop, size=100.0, token_out=109.0, legs=legs)
    rec = await _run(loop, sim)
    assert rec.state == LoopState.DONE
    assert rec.steps_done == [
        "platform_sell_token", "withdraw_usdc", "bridge_ethereum_base",
        "onchain_buyback", "vnx_token_deposit",
    ]


@pytest.mark.asyncio
async def test_loop3_base_to_solana_cctp():
    loop = LoopSpec(LOOP3_CROSS, "VCHF", "base", "solana")
    legs = [
        LoopLeg("sell_onchain", "base", "", 145.0),
        LoopLeg("bridge_stable", "base", "", 143.0),
        LoopLeg("onchain_buyback", "solana", "", 143.0),
        LoopLeg("vnx_token_deposit", "solana", "", 143.0),
    ]
    sim = _sim(loop, size=100.0, token_out=107.0, legs=legs)
    rec = await _run(loop, sim)
    assert rec.state == LoopState.DONE
    assert "bridge_base_solana" in rec.steps_done


@pytest.mark.asyncio
async def test_loop3_celo_to_base_eth_triangle_not_wired():
    loop = LoopSpec(LOOP3_CROSS, "VCHF", "celo", "base")
    legs = [
        LoopLeg("sell_onchain", "celo", "", 145.0),
        LoopLeg("bridge_stable", "celo", "", 143.0),
        LoopLeg("onchain_buyback", "base", "", 143.0),
        LoopLeg("vnx_token_deposit", "base", "", 143.0),
    ]
    sim = _sim(loop, size=100.0, token_out=107.0, legs=legs)
    rec = await _run(loop, sim)
    assert rec.state == LoopState.FAILED
    assert rec.error and "eth_triangle" in rec.error
    # withdraw + sell happened before the unsupported bridge leg
    assert rec.steps_done == ["withdraw_token", "sell_token_onchain"]


@pytest.mark.asyncio
async def test_unprofitable_loop_is_gated():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "base")
    sim = _sim(loop, size=100.0, token_out=99.0, legs=_l1_legs("base"), profitable=False, error="loss")
    rec = await _run(loop, sim)
    assert rec.state == LoopState.FAILED
    assert rec.error == "loss"


@pytest.mark.asyncio
async def test_live_execution_blocked_without_flag():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "base")
    sim = _sim(loop, size=100.0, token_out=110.0, legs=_l1_legs("base"))
    rec = await _run(loop, sim, dry_run=False, enable=False)
    assert rec.state == LoopState.FAILED
    assert rec.error and "ENABLE_LOOP_EXECUTOR" in rec.error


@pytest.mark.asyncio
async def test_live_execution_allowed_with_flag():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "base")
    sim = _sim(loop, size=100.0, token_out=110.0, legs=_l1_legs("base"))
    rec = await _run(loop, sim, dry_run=False, enable=True)
    assert rec.state == LoopState.DONE
