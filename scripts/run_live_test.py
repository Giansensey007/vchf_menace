#!/usr/bin/env python3
"""
Sequential live integration test for VCHF Menace.

Usage:
  python scripts/run_live_test.py              # audit balances + dry-run checks
  python scripts/run_live_test.py --execute    # live legs (requires DRY_RUN=false)

Order:
  1. Platform audit (CHF/USDC/VCHF)
  2. CHF→USDC conversion (if CHF >= 30 USDC worth)
  3. Platform VCHF buy/sell probe (30 VCHF min)
  4. Celo + Sol swap probes (5 VCHF)
  5. CCTP round-trip probe ($10 USDC) — optional with --cctp
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.cctp import CircleCctpBridge
from src.config_loader import is_dry_run, load_bot_config, load_chains, load_tokens, token_decimals
from src.execution.celo import CeloExecutor
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.http_client import build_client
from src.quotes.types import from_human, to_human
from src.vnx.client import VnxClient
from src.vnx.trading import platform_buy_vchf, platform_sell_vchf

PROBE_VCHF = 5.0
CCTP_PROBE_USDC = 10.0
PLATFORM_PROBE_VCHF = 30.0


def _ok(label: str, detail: str = "") -> None:
    print(f"  OK  {label}" + (f": {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL {label}" + (f": {detail}" if detail else ""))


async def audit_balances() -> bool:
    print("\n=== 1. Balance audit ===")
    chains = load_chains()
    token = load_tokens()["VCHF"]
    ok = True

    try:
        celo = CeloExecutor(chains["celo"])
        usdt = to_human(celo.balance_erc20(chains["celo"].hub_token), 6)
        native = to_human(celo.balance_native(), 18)
        _ok("Celo", f"{celo.address[:10]}… USDT={usdt:.2f} CELO={native:.4f}")
    except Exception as exc:
        _fail("Celo", str(exc))
        ok = False

    try:
        sol = SolanaExecutor(chains["solana"])
        lam = sol.balance_lamports() / 1e9
        _ok("Solana", f"{sol.pubkey[:10]}… SOL={lam:.4f}")
    except Exception as exc:
        _fail("Solana", str(exc))
        ok = False

    try:
        eth = EthereumExecutor(chains["ethereum"])
        usdc_eth = to_human(eth.balance_erc20(chains["ethereum"].hub_token), 6)
        eth_native = to_human(eth.balance_native(), 18)
        _ok("Ethereum", f"{eth.address[:10]}… USDC={usdc_eth:.2f} ETH={eth_native:.4f}")
        if usdc_eth < 1:
            print("  WARN: ETH wallet has <1 USDC — CCTP ETH→Sol needs mainnet USDC on same key as CELO")
    except Exception as exc:
        _fail("Ethereum", str(exc))
        ok = False

    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        chf = vnx._asset_balance(bal, "CHF")
        usdc = vnx._asset_balance(bal, "USDC")
        vchf = vnx.vchf_balance(bal)
        _ok("VNX Platform", f"CHF={chf:.2f} USDC={usdc:.2f} VCHF={vchf:.2f}")

    vchf_dec = token_decimals(token, "celo")
    try:
        celo_vchf = to_human(CeloExecutor(chains["celo"]).balance_erc20(token.chains["celo"]), vchf_dec)
        _ok("Celo VCHF on-chain", f"{celo_vchf:.2f}")
    except Exception:
        pass

    return ok


async def run_platform_convert(execute: bool) -> bool:
    print("\n=== 2. Platform CHF→USDC ===")
    if not execute:
        print("  SKIP (dry-run). Use --execute to convert.")
        return True
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "scripts/convert_platform_chf.py"),
        "--execute",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    print(out.decode())
    return proc.returncode == 0


async def run_platform_vchf(execute: bool) -> bool:
    print("\n=== 3. Platform VCHF buy/sell probe ===")
    cfg = load_bot_config()
    if not execute:
        print(f"  SKIP live (would trade {PLATFORM_PROBE_VCHF} VCHF)")
        return True

    async with VnxClient() as vnx:
        async with build_client() as client:
            buy = await platform_buy_vchf(cfg, PLATFORM_PROBE_VCHF, vnx=vnx)
            if not buy.success:
                _fail("platform buy", buy.error or "failed")
                return False
            _ok("platform buy", buy.clordid)

            sell = await platform_sell_vchf(cfg, PLATFORM_PROBE_VCHF, vnx=vnx)
            if not sell.success:
                _fail("platform sell", sell.error or "failed")
                return False
            _ok("platform sell", sell.clordid)
    return True


async def run_swap_probes(execute: bool) -> bool:
    print("\n=== 4. On-chain swap probes (5 VCHF) ===")
    if not execute:
        print("  SKIP (dry-run)")
        return True
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "scripts/test_probe_trades.py"),
        "--execute",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    print(out.decode()[-3000:])
    return proc.returncode == 0


async def run_cctp_probe(execute: bool) -> bool:
    print(f"\n=== 5. CCTP probe (${CCTP_PROBE_USDC} USDC) ===")
    bridge = CircleCctpBridge()
    async with build_client() as client:
        q = await bridge.quote_usdc(client, "solana", "ethereum", CCTP_PROBE_USDC)
        _ok("CCTP quote Sol→ETH", f"fee=${q.fee_usd:.4f} out=${q.amount_out_usdc:.2f}")

        if not execute:
            print("  SKIP live CCTP (use --execute --cctp)")
            return True

        if is_dry_run():
            _fail("CCTP", "set DRY_RUN=false")
            return False

        # Sol → ETH then ETH → Sol (small round trip)
        r1 = await bridge.bridge_usdc_sol_to_eth(client, CCTP_PROBE_USDC)
        if not r1.success:
            _fail("CCTP Sol→ETH", r1.error or "failed")
            return False
        _ok("CCTP Sol→ETH", f"src={r1.source_tx} dst={r1.dest_tx}")

        await asyncio.sleep(5)
        r2 = await bridge.bridge_usdc_eth_to_sol(client, CCTP_PROBE_USDC * 0.95)
        if not r2.success:
            _fail("CCTP ETH→Sol", r2.error or "failed")
            return False
        _ok("CCTP ETH→Sol", f"src={r2.source_tx} dst={r2.dest_tx}")
    return True


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true", help="Run live legs")
    p.add_argument("--cctp", action="store_true", help="Include CCTP round-trip (costs ~$20+ fees)")
    args = p.parse_args()

    print("VCHF Menace live test")
    print(f"  DRY_RUN={os.getenv('DRY_RUN', 'true')}")
    print(f"  VNX labels: CELO={os.getenv('VNX_CELO_WITHDRAW_LABEL')} SOL={os.getenv('VNX_SOL_WITHDRAW_LABEL')}")

    if args.execute and is_dry_run():
        print("\nERROR: --execute requires DRY_RUN=false in .env")
        sys.exit(1)

    ok = await audit_balances()
    ok = await run_platform_convert(args.execute) and ok
    ok = await run_platform_vchf(args.execute) and ok
    ok = await run_swap_probes(args.execute) and ok
    if args.cctp:
        ok = await run_cctp_probe(args.execute) and ok

    print("\n=== Result ===")
    print("PASS" if ok else "FAIL — see errors above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
