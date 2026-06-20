#!/usr/bin/env python3
"""Live route: Platform VCHF → BASE → USDT → Sol → VCHF → platform."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from scripts.execute_route_matrix import (
    audit,
    step_cctp_claim,
    step_wormhole_claim,
)
from src.config_loader import load_bot_config, load_chains, load_tokens
from src.db import init_db
from src.execution.executor import ArbExecutor, CycleState
from src.execution.tx_log import TX_LOG_PATH, log_tx
from src.quotes.http_client import build_client
from src.treasury.manager import TreasuryManager
from src.vnx.bridge import VCHF_WITHDRAW_FEE_BUFFER
from src.vnx.client import VnxClient
from src.vnx.trading import VCHF_MIN_ORDER, VCHF_USDC_QTY_DECIMALS, _round_down


def _log(msg: str) -> None:
    print(msg, flush=True)


def _collect_txs(record) -> list[str]:
    if not record:
        return []
    return [t for t in record.tx_hashes if t and not t.startswith("dry-run")]


async def _platform_balances() -> dict[str, float]:
    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        return {
            "usdc": vnx.usdc_balance(bal),
            "vchf": vnx.vchf_balance(bal),
            "chf": vnx._asset_balance(bal, "CHF"),
        }


def _size_from_platform(vchf: float, usdc: float, target: float = 31.0) -> tuple[float, str]:
    withdrawable = max(0.0, vchf - VCHF_WITHDRAW_FEE_BUFFER)
    if vchf >= target * 0.95:
        return target, f"platform VCHF {vchf:.2f} — using target {target:.2f}"
    if withdrawable >= 5.0:
        size = _round_down(withdrawable, VCHF_USDC_QTY_DECIMALS)
        return size, f"withdraw-only {size:.2f} VCHF (balance {vchf:.2f} − fee buffer {VCHF_WITHDRAW_FEE_BUFFER})"
    need_usdc = VCHF_MIN_ORDER * 1.35
    if usdc >= need_usdc * 0.95:
        return VCHF_MIN_ORDER, f"buy minimum {VCHF_MIN_ORDER:.0f} VCHF (USDC {usdc:.2f})"
    raise RuntimeError(
        f"Insufficient platform funds: VCHF={vchf:.2f} withdrawable={withdrawable:.2f} "
        f"USDC={usdc:.2f} (need ≥5 withdrawable or ≥{need_usdc:.0f} USDC for buy min)"
    )


async def run_route() -> int:
    init_db()
    if TX_LOG_PATH.exists():
        TX_LOG_PATH.write_text("", encoding="utf-8")

    _log("\n========== LIVE ROUTE: VNX → BASE → SOL → VNX ==========")
    _log("=== BEFORE audit ===")
    await audit()
    before = await _platform_balances()
    _log(f"Platform snapshot: {before}")

    size, sizing_note = _size_from_platform(before["vchf"], before["usdc"])
    _log(f"\nRoute size: {size:.2f} VCHF — {sizing_note}")

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    treasury = TreasuryManager(chains, token, cfg)
    ex = ArbExecutor(chains, token, cfg)

    all_txs: dict[str, list[str]] = {}
    results: dict[str, bool] = {}

    async with build_client() as client:
        await step_cctp_claim()
        await step_wormhole_claim(max_rounds=20)

        # Leg 1: withdraw VCHF → Base, sell for USDT
        _log(f"\n--- Leg 1: vnx_to_base @ {size:.2f} VCHF ---")
        prep1 = await treasury.prepare_for_direction("vnx_to_base", size)
        _log(f"  prep: ready={prep1.ready} size={prep1.size_vchf:.2f} notes={prep1.notes}")
        if not prep1.ready:
            _log(f"ABORT leg1: {prep1.notes}")
            return 1
        leg1_size = prep1.size_vchf
        r1 = await ex.run_cycle(client, "vnx_to_base", leg1_size, force_execute=True)
        all_txs["vnx_to_base"] = _collect_txs(r1)
        results["vnx_to_base"] = r1.state == CycleState.DONE
        _log(f"  leg1 state={r1.state.value} txs={all_txs['vnx_to_base']} err={r1.error}")
        if r1.state != CycleState.DONE:
            return 1

        await step_wormhole_claim(max_rounds=30)
        await step_cctp_claim()
        await audit()

        # Leg 2: Base USDT → Sol USDC (cross-chain arb)
        _log(f"\n--- Leg 2: base_to_solana @ {leg1_size:.2f} VCHF ---")
        prep2 = await treasury.prepare_for_direction("base_to_solana", leg1_size)
        _log(f"  prep: ready={prep2.ready} size={prep2.size_vchf:.2f} notes={prep2.notes}")
        if not prep2.ready:
            _log(f"ABORT leg2: {prep2.notes}")
            return 1
        leg2_size = prep2.size_vchf
        r2 = await ex.run_cycle(client, "base_to_solana", leg2_size, force_execute=True)
        all_txs["base_to_solana"] = _collect_txs(r2)
        results["base_to_solana"] = r2.state == CycleState.DONE
        _log(f"  leg2 state={r2.state.value} txs={all_txs['base_to_solana']} err={r2.error}")
        if r2.state != CycleState.DONE:
            return 1

        await step_wormhole_claim(max_rounds=60)
        await step_cctp_claim()
        await audit()

        # Leg 3: Sol USDC → buy VCHF → deposit platform
        _log(f"\n--- Leg 3: solana_to_vnx @ {leg2_size:.2f} VCHF ---")
        prep3 = await treasury.prepare_for_direction("solana_to_vnx", leg2_size)
        _log(f"  prep: ready={prep3.ready} size={prep3.size_vchf:.2f} notes={prep3.notes}")
        if not prep3.ready:
            _log(f"ABORT leg3: {prep3.notes}")
            return 1
        leg3_size = prep3.size_vchf
        r3 = await ex.run_cycle(client, "solana_to_vnx", leg3_size, force_execute=True)
        all_txs["solana_to_vnx"] = _collect_txs(r3)
        results["solana_to_vnx"] = r3.state == CycleState.DONE
        _log(f"  leg3 state={r3.state.value} txs={all_txs['solana_to_vnx']} err={r3.error}")

    await step_wormhole_claim(max_rounds=30)
    await step_cctp_claim()

    _log("\n=== AFTER audit ===")
    await audit()
    after = await _platform_balances()
    _log(f"Platform snapshot: {after}")

    closed = all(results.values())
    _log("\n=== ROUTE SUMMARY ===")
    _log(f"  size_used={leg1_size:.2f} VCHF")
    _log(f"  platform_vchf_before={before['vchf']:.2f} after={after['vchf']:.2f}")
    _log(f"  platform_usdc_before={before['usdc']:.2f} after={after['usdc']:.2f}")
    _log(f"  closed_success={closed}")
    for leg, txs in all_txs.items():
        _log(f"  {leg}: {len(txs)} tx(s)")
        for tx in txs:
            _log(f"    {tx}")

    if TX_LOG_PATH.exists():
        _log(f"\n=== TX log ({TX_LOG_PATH}) ===")
        for line in TX_LOG_PATH.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            url = row.get("url") or ""
            _log(f"  {row.get('intent')} | {row.get('chain')} | {row.get('tx_hash')} {url}")

    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_route()))
