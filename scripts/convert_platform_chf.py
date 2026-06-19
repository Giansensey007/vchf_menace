#!/usr/bin/env python3
"""
Convert platform CHF balance → USDC via VNX addOrder (USDC/CHF, FOK).

Minimum order: 30 USDC (~25 CHF at current rates). Run after CHF top-up.

Usage:
  python scripts/convert_platform_chf.py           # dry-run estimate
  python scripts/convert_platform_chf.py --execute # live order (DRY_RUN ignored)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.vnx.client import VnxClient

USDC_CHF = "USDC/CHF"
MIN_USDC = 30.0
# Conservative limit price (CHF per USDC) — FOK uses platform quotes internally
DEFAULT_PRICE = 0.92


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _pair_meta(vnx: VnxClient) -> dict:
    pairs = await vnx.get_trading_pairs()
    for row in pairs.get("pairs") or []:
        if row.get("pair") == USDC_CHF:
            return row
    return {}


async def main() -> None:
    p = argparse.ArgumentParser(description="Convert platform CHF → USDC")
    p.add_argument("--execute", action="store_true", help="Place live FOK buy order")
    p.add_argument("--usdc", type=float, default=MIN_USDC, help=f"USDC qty (min {MIN_USDC})")
    p.add_argument("--price", type=float, default=DEFAULT_PRICE, help="Limit price CHF/USDC")
    args = p.parse_args()

    usdc_qty = max(args.usdc, MIN_USDC)
    est_chf = usdc_qty * args.price * 1.002  # small buffer for fees/slippage

    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        chf = vnx._asset_balance(bal, "CHF")
        usdc = vnx._asset_balance(bal, "USDC")
        meta = await _pair_meta(vnx)
        min_sz = float(meta.get("min_order_size") or MIN_USDC)

        print("=== VNX platform balances ===")
        print(f"  CHF:  {chf:.2f}")
        print(f"  USDC: {usdc:.2f}")
        print(f"  USDC/CHF min order: {min_sz} USDC")
        print(f"\nPlan: Buy {usdc_qty:.2f} USDC @ limit {args.price} CHF/USDC")
        print(f"  Estimated CHF needed: ~{est_chf:.2f}")

        if chf < est_chf:
            shortfall = est_chf - chf
            print(f"\nBLOCKED: need ~{shortfall:.2f} more CHF on platform")
            print(f"  Top up to at least {est_chf:.0f} CHF total, then re-run with --execute")
            sys.exit(1)

        if not args.execute:
            print("\nDry-run only. Re-run with --execute to place order.")
            return

        payload = {
            "timestamp": _utc_ts(),
            "clordid": f"chf2usdc-{uuid.uuid4().hex[:12]}",
            "symbol": USDC_CHF,
            "side": "Buy",
            "ordtype": "Limit",
            "timeinforce": "FOK",
            "orderqty": usdc_qty,
            "price": args.price,
        }
        data = await vnx.add_order(payload)
        if data.get("result") != "success":
            err = data.get("error") or {}
            print(f"\nORDER FAILED: {err.get('message') or err.get('code') or data}")
            sys.exit(1)

        order = data.get("order") or {}
        print("\nORDER OK")
        print(f"  ordid: {order.get('ordid')} status: {order.get('ordstatus')}")
        print(f"  bought: {order.get('bought')} {order.get('bought_currency')}")
        print(f"  sold: {order.get('sold')} {order.get('sold_currency')}")
        print(f"  fee: {order.get('fee')} {order.get('fee_currency')}")

        bal2 = await vnx.account_balance()
        print("\nBalances after:")
        print(f"  CHF:  {vnx._asset_balance(bal2, 'CHF'):.2f}")
        print(f"  USDC: {vnx._asset_balance(bal2, 'USDC'):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
