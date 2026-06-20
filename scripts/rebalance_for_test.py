#!/usr/bin/env python3
"""
Rebalance platform + chains before full route test.

Targets (platform-centric treasury):
  - Platform: >= 32 VCHF (all inventory) + >= 54 USDC for vnx_to_* buys
  - Solana:   >= 53 USDC only (no on-chain VCHF)
  - Celo:     >= 53 USDT only (no on-chain VCHF)
  - Ethereum: >= 3 USDC buffer + gas ETH (hub only)

Usage:
  python scripts/rebalance_for_test.py           # audit + plan
  python scripts/rebalance_for_test.py --execute # live moves
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.cctp import CircleCctpBridge
from src.bridge.cctp_queue import CctpClaimQueue
from src.bridge.hub_eth import eth_usdc_to_vnx, wormhole_celo_to_eth, wormhole_eth_to_celo
from src.bridge.wormhole_queue import WormholeClaimQueue
from src.treasury.manager import TreasuryManager
from src.config_loader import load_bot_config, load_bridge_config, load_chains, load_tokens, token_decimals
from src.execution.base import BaseExecutor
from src.execution.executor import ArbExecutor, CycleRecord, CycleState
from src.execution.solana import SolanaExecutor
from src.execution.tx_log import log_platform_order, log_tx
from src.quotes.http_client import build_client
from src.quotes.types import from_human, to_human
from src.vnx.deposits import check_usdc_deposit_amount, min_deposit_usdc
from src.vnx.client import VnxClient
from src.vnx.trading import _round_down, platform_buy_vchf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("rebalance")

TEST_VCHF = 31.0
VCHF_BUFFER = 1.0  # withdraw fee
USDC_FOR_BUY = 53.0  # ~31 VCHF + slippage on Jupiter/VNX
USDT_FOR_BUY = 53.0
USDC_NEAR = USDC_FOR_BUY * 0.95  # tolerate ~5% shortfall for sequential routes
USDT_NEAR = USDT_FOR_BUY * 0.95
MIN_ETH_USDC = 3.0
MIN_ETH_USDT = 5.0
HUB_USDC = min_deposit_usdc("ETH")  # VNX ETH USDC cumulative minimum (default 20)
HUB_USDT = 5.0
MIN_SOL = 0.03


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _balances() -> dict[str, float]:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    out: dict[str, float] = {}

    for attempt in range(3):
        try:
            async with VnxClient() as vnx:
                bal = await vnx.account_balance()
                out["platform_usdc"] = vnx.usdc_balance(bal)
                out["platform_vchf"] = vnx.vchf_balance(bal)
                out["platform_chf"] = vnx._asset_balance(bal, "CHF")
            break
        except Exception as exc:
            if attempt == 2:
                raise
            _log(f"  VNX balance retry ({exc})")
            time.sleep(2)

    celo = BaseExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    out["celo_usdt"] = float(to_human(celo.balance_erc20(chains["celo"].hub_token), 6))
    out["celo_vchf"] = float(to_human(celo.balance_erc20(token.chains["celo"]), dec))

    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    usdc_ata = get_associated_token_address(
        sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token)
    )
    vchf_ata = get_associated_token_address(
        sol.keypair.pubkey(), Pubkey.from_string(token.chains["solana"])
    )
    for attempt in range(5):
        try:
            out["sol_usdc"] = float(sol.client.get_token_account_balance(usdc_ata).value.ui_amount or 0)
            try:
                out["sol_vchf"] = float(sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0)
            except Exception:
                out["sol_vchf"] = 0.0
            out["sol_native"] = sol.balance_lamports() / 1e9
            break
        except Exception as exc:
            if attempt == 4:
                _log(f"  Sol balance unavailable after retries: {exc}")
                out["sol_usdc"] = 0.0
                out["sol_vchf"] = 0.0
                out["sol_native"] = 0.0
            else:
                time.sleep(3 * (attempt + 1))

    try:
        from src.execution.ethereum import EthereumExecutor

        eth = EthereumExecutor(chains["ethereum"])
        wh = load_bridge_config()["wormhole"]
        out["eth_usdc"] = float(to_human(eth.balance_erc20(chains["ethereum"].hub_token), 6))
        out["eth_usdt"] = float(to_human(eth.balance_erc20(wh["ethereum_usdt"]), 6))
        out["eth_native"] = eth.balance_native() / 1e18
    except Exception as exc:
        _log(f"ETH unavailable: {exc}")
        out["eth_usdc"] = 0.0
        out["eth_usdt"] = 0.0
        out["eth_native"] = 0.0

    return out


async def route_order_for_balances(b: dict[str, float], *, dust: float) -> list[str]:
    """Pick capital-efficient route order from current balances."""
    need_plat = b["platform_usdc"] < USDC_NEAR and b["platform_vchf"] < TEST_VCHF + VCHF_BUFFER
    sol_can_fund_plat = b["sol_usdc"] >= USDC_NEAR
    celo_ready = b["celo_usdt"] >= USDT_NEAR
    if celo_ready and not (b["platform_vchf"] >= TEST_VCHF or b["platform_usdc"] >= USDC_NEAR):
        return ["base_to_solana", "solana_to_base", "solana_to_vnx", "vnx_to_solana"]
    if need_plat and sol_can_fund_plat:
        return ["solana_to_vnx", "vnx_to_solana", "solana_to_base", "base_to_solana"]
    return ["vnx_to_solana", "solana_to_vnx", "solana_to_base", "base_to_solana"]


def route_ready(direction: str, b: dict[str, float], *, dust: float) -> tuple[bool, str]:
    """Check if a single route can run at TEST_VCHF with platform-centric capital."""
    if direction == "vnx_to_solana":
        ok = b["platform_vchf"] >= TEST_VCHF or b["platform_usdc"] >= USDC_NEAR
        return ok, "platform VCHF or USDC"
    if direction == "vnx_to_base":
        ok = b["platform_vchf"] >= TEST_VCHF or b["platform_usdc"] >= USDC_NEAR
        return ok, "platform VCHF or USDC"
    if direction == "solana_to_vnx":
        ok = b["sol_usdc"] >= USDC_NEAR and b["celo_vchf"] <= dust and b["sol_vchf"] <= dust
        return ok, "Sol USDC (no on-chain VCHF)"
    if direction == "base_to_vnx":
        ok = b["celo_usdt"] >= USDT_NEAR and b["celo_vchf"] <= dust
        return ok, "Celo USDT (no on-chain VCHF)"
    if direction == "solana_to_base":
        ok = b["sol_usdc"] >= USDC_NEAR
        return ok, "Sol USDC"
    if direction == "base_to_solana":
        ok = b["celo_usdt"] >= USDT_NEAR
        return ok, "Celo USDT"
    return False, "unknown route"


async def audit() -> dict[str, float]:
    bot_cfg = load_bot_config()
    dust = bot_cfg.vchf_on_chain_dust
    b = await _balances()
    _log("\n=== Balance audit ===")
    _log(
        f"Platform: USDC={b['platform_usdc']:.2f} VCHF={b['platform_vchf']:.2f} CHF={b['platform_chf']:.2f}"
    )
    _log(f"Celo: USDT={b['celo_usdt']:.2f} VCHF={b['celo_vchf']:.2f}")
    _log(f"Sol:  USDC={b['sol_usdc']:.2f} VCHF={b['sol_vchf']:.2f} SOL={b['sol_native']:.4f}")
    _log(f"ETH:  USDC={b['eth_usdc']:.2f} USDT={b['eth_usdt']:.2f} ETH={b['eth_native']:.4f}")
    _log("\n=== Targets (platform-centric) ===")
    _log(f"  Platform: USDC>={USDC_FOR_BUY} or VCHF>={TEST_VCHF + VCHF_BUFFER}")
    _log(f"  Sol:      USDC>={USDC_FOR_BUY} (no on-chain VCHF > {dust})")
    _log(f"  Celo:     USDT>={USDT_FOR_BUY} (no on-chain VCHF > {dust})")
    if b["celo_vchf"] > dust or b["sol_vchf"] > dust:
        _log(
            f"WARN: on-chain VCHF celo={b['celo_vchf']:.2f} sol={b['sol_vchf']:.2f} "
            f"(dust={dust}) — consolidate to platform"
        )
    order = await route_order_for_balances(b, dust=dust)
    _log(f"\nSuggested route order: {order}")
    for d in order:
        ok, need = route_ready(d, b, dust=dust)
        _log(f"  {d}: {'OK' if ok else 'NEED'} ({need})")
    if b["platform_usdc"] < USDC_FOR_BUY and b["platform_chf"] < 25:
        _log("\nNOTE: Platform low on USDC; CHF→USDC needs ≥25 CHF (min 30 USDC order).")
    return b


async def step_cctp_claim(*, discover: bool | None = None) -> None:
    import os

    interval = float(os.getenv("CCTP_CLAIM_INTERVAL_SEC", "30"))
    max_rounds = int(os.getenv("CCTP_CLAIM_MAX_ROUNDS", "120"))
    _log("\n--- CCTP claim queue ---")
    queue = CctpClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(
            client,
            interval_sec=interval,
            max_rounds=max_rounds,
            discover_first=True if discover is None else discover,
        )
    _log(f"CCTP: claimed={summary['claimed']} remaining={summary['remaining']}")


async def step_wormhole_claim(*, max_rounds: int = 60) -> None:
    interval = float(os.getenv("WORMHOLE_CLAIM_INTERVAL_SEC", "30"))
    _log("\n--- Wormhole claim queue ---")
    queue = WormholeClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(client, interval_sec=interval, max_rounds=max_rounds)
    _log(f"Wormhole: claimed={summary['claimed']} remaining={summary['remaining']}")


async def step_cctp_eth_to_sol(client, amount: float) -> bool:
    if amount < 5:
        return True
    _log(f"\n--- CCTP ETH→Sol ${amount:.2f} USDC ---")
    try:
        bridge = CircleCctpBridge()
        r = await bridge.bridge_usdc_eth_to_sol(client, amount)
        _log(f"  {'OK' if r.success else 'PARTIAL'} src={r.source_tx} dst={r.dest_tx} err={r.error}")
        if r.source_tx and not r.dest_tx:
            await step_cctp_claim()
        return r.success or bool(r.dest_tx) or bool(r.source_tx)
    except Exception as exc:
        _log(f"  FAIL CCTP ETH→Sol: {exc}")
        return False


async def _fund_chain_stable_via_vnx(
    client,
    treasury: TreasuryManager,
    executor: ArbExecutor,
    chain: str,
    size_vchf: float,
    execute: bool,
) -> bool:
    """
    Fund Celo USDT or Sol USDC via vnx_to_* — withdraws platform VCHF, sells to stable on chain.
    Ends with VCHF consolidated back on platform (no on-chain VCHF inventory).
    """
    direction = f"vnx_to_{chain}"
    _log(f"\n--- Treasury fund: {direction} ({size_vchf:.0f} VCHF) → {chain} stable ---")
    if not execute:
        return True
    prep = await treasury.prepare_for_direction(direction, size_vchf)
    if not prep.ready:
        _log(f"  SKIP: {prep.notes}")
        return False
    from src.scanner.simulator import simulate_direction

    sim = await simulate_direction(
        client, load_chains(), load_tokens()["VCHF"], load_bot_config(), direction, size_vchf
    )
    record = CycleRecord(id="rebal", direction=direction, size_vchf=size_vchf)
    record.simulation = sim
    record.state = CycleState.EXECUTING
    await executor._exec_vnx_to_chain(client, record, sim, chain)
    await treasury.consolidate_vchf_to_platform()
    ok = record.state == CycleState.DONE
    _log(f"  {'OK' if ok else 'FAIL'} {direction} err={record.error}")
    return ok


async def rebalance(execute: bool) -> bool:
    os.environ["CCTP_RECONCILE_USDC"] = "0"
    cfg = load_bot_config()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    treasury = TreasuryManager(chains, token, cfg)
    executor = ArbExecutor(chains, token, cfg)

    await step_cctp_claim()
    await step_wormhole_claim()
    if execute:
        await treasury.consolidate_vchf_to_platform()
    b = await audit()
    _log("\n=== Treasury policy: VCHF on platform only; chains hold stables ===")

    ok = True
    async with build_client() as client:
        # Move excess ETH USDC to platform when low on platform USDC
        if b["platform_usdc"] < USDC_FOR_BUY * 0.9 and b["eth_usdc"] > MIN_ETH_USDC + HUB_USDC:
            move = min(b["eth_usdc"] - MIN_ETH_USDC, HUB_USDC)
            dep_err = check_usdc_deposit_amount("ETH", move)
            if dep_err:
                _log(f"\nSKIP eth→vnx: {dep_err}")
            elif execute and move >= HUB_USDC:
                _log(f"\n--- ETH USDC → VNX platform ${move:.2f} ---")
                r = await eth_usdc_to_vnx(client, move)
                if not r["success"]:
                    _log(f"  FAIL eth→vnx: {r.get('error')}")
                    ok = False
            elif move >= HUB_USDC:
                _log(f"\nPLAN: ETH USDC → VNX ${move:.2f}")
            b = await _balances()

        # Fund Celo USDT from ETH via Wormhole when Celo short and ETH has USDT
        if b["celo_usdt"] < USDT_FOR_BUY * 0.5 and b["eth_usdt"] >= HUB_USDT + MIN_ETH_USDT:
            move = min(HUB_USDT, b["eth_usdt"] - MIN_ETH_USDT)
            if execute and move >= HUB_USDT * 0.9:
                _log(f"\n--- Wormhole ETH→Celo ${move:.2f} USDT ---")
                r = await wormhole_eth_to_celo(client, move)
                if not r["success"]:
                    _log(f"  FAIL eth→celo: {r.get('error')}")
                    ok = False
                await step_wormhole_claim(max_rounds=40)
            elif move >= HUB_USDT * 0.9:
                _log(f"\nPLAN: Wormhole ETH→Celo ${move:.2f} USDT")
            b = await _balances()

        # Fund ETH USDT from Celo via Wormhole when platform/ETH need stables
        if (
            b["eth_usdt"] < MIN_ETH_USDT
            and b["celo_usdt"] >= HUB_USDT + 5
            and b["platform_usdc"] < USDC_NEAR
        ):
            move = min(HUB_USDT, b["celo_usdt"] - 5)
            if execute and move >= HUB_USDT * 0.9:
                _log(f"\n--- Wormhole Celo→ETH ${move:.2f} USDT ---")
                r = await wormhole_celo_to_eth(client, move)
                if not r["success"]:
                    _log(f"  FAIL celo→eth: {r.get('error')}")
                    ok = False
                await step_wormhole_claim(max_rounds=40)
            elif move >= HUB_USDT * 0.9:
                _log(f"\nPLAN: Wormhole Celo→ETH ${move:.2f} USDT")
            b = await _balances()

        # Move excess ETH USDC to Sol (keep MIN_ETH_USDC on ETH)
        if b["eth_usdc"] > MIN_ETH_USDC + 5:
            move = min(b["eth_usdc"] - MIN_ETH_USDC, 15.0)
            if execute:
                if not await step_cctp_eth_to_sol(client, move):
                    ok = False
            else:
                _log(f"\nPLAN: CCTP ETH→Sol ${move:.2f}")

        b = await _balances()

        # Fund Sol USDC from ETH if still low
        if b["sol_usdc"] < USDC_FOR_BUY and b["eth_usdc"] > MIN_ETH_USDC + 5:
            need = min(USDC_FOR_BUY - b["sol_usdc"], b["eth_usdc"] - MIN_ETH_USDC)
            if execute and need >= 5:
                if not await step_cctp_eth_to_sol(client, need):
                    ok = False
            elif need >= 5:
                _log(f"\nPLAN: CCTP ETH→Sol ${need:.2f} (Sol USDC low)")
            b = await _balances()

        # Platform VCHF inventory (home for all VCHF)
        need_vchf_plat = TEST_VCHF + VCHF_BUFFER
        if b["platform_vchf"] < need_vchf_plat and b["platform_usdc"] >= USDC_FOR_BUY * 0.95:
            if execute:
                buy = await platform_buy_vchf(cfg, TEST_VCHF, max_usdc=b["platform_usdc"] * 0.995)
                if buy.success:
                    log_platform_order("rebalance_platform_vchf", buy.ordid, qty=buy.quantity)
                else:
                    _log(f"FAIL platform VCHF buy: {buy.error}")
                    ok = False
            else:
                _log(f"\nPLAN: Platform buy VCHF (~{need_vchf_plat - b['platform_vchf']:.0f})")
            b = await _balances()

        # Fund Celo USDT via vnx_to_base (ends with USDT on Celo, VCHF back on platform)
        if b["celo_usdt"] < USDT_FOR_BUY and b["platform_vchf"] >= TEST_VCHF:
            if execute:
                if not await _fund_chain_stable_via_vnx(client, treasury, executor, "celo", TEST_VCHF, True):
                    ok = False
                else:
                    time.sleep(30)
            else:
                _log(f"\nPLAN: vnx_to_base {TEST_VCHF} VCHF → Celo USDT")
            b = await _balances()

        # Fund Sol USDC via vnx_to_solana
        if b["sol_usdc"] < USDC_FOR_BUY and b["platform_vchf"] >= TEST_VCHF:
            if execute:
                if not await _fund_chain_stable_via_vnx(client, treasury, executor, "solana", TEST_VCHF, True):
                    ok = False
                else:
                    time.sleep(30)
            else:
                _log(f"\nPLAN: vnx_to_solana {TEST_VCHF} VCHF → Sol USDC")
            b = await _balances()

        if execute:
            await treasury.consolidate_vchf_to_platform()

    await step_cctp_claim()
    await step_wormhole_claim()
    b = await audit()

    bot_cfg = load_bot_config()
    dust = bot_cfg.vchf_on_chain_dust
    if b["celo_vchf"] > dust or b["sol_vchf"] > dust:
        _log(
            f"WARN: on-chain VCHF celo={b['celo_vchf']:.2f} sol={b['sol_vchf']:.2f} "
            f"(dust={dust}) — run consolidate"
        )

    order = await route_order_for_balances(b, dust=dust)
    first_ok, first_need = route_ready(order[0], b, dust=dust)
    needs_eth_buffer = order[0] in ("vnx_to_solana", "solana_to_vnx") and b["platform_usdc"] < USDC_NEAR
    ready = b["sol_native"] >= MIN_SOL and first_ok
    if needs_eth_buffer:
        ready = ready and b["eth_usdc"] >= MIN_ETH_USDC
    if not first_ok:
        _log(f"\nFirst route ({order[0]}) blocked: need {first_need}")
    _log(f"\n{'READY' if ready else 'NOT READY'} for full 31 VCHF route test")
    return ready and ok


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    ok = await rebalance(args.execute)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
