#!/usr/bin/env python3
"""Audit VNX bridge API auth and addresses."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.vnx.auth import ensure_public_key_env
from src.vnx.client import VnxClient


async def main() -> None:
    try:
        ensure_public_key_env()
    except Exception as exc:
        print(f"Auth setup failed: {exc}")
        sys.exit(1)

    async with VnxClient() as vnx:
        try:
            assets = await vnx.get_assets()
            vchf = next((a for a in assets.get("assets", []) if a.get("asset") == "VCHF"), None)
            print("VCHF asset:", vchf)
        except Exception as exc:
            print(f"WARN: /client/assets failed ({exc})")
            print("Set VNX_API_PUBLIC_KEY from VNX Platform My Account if derived key gets 401.")

        try:
            bal = await vnx.account_balance()
            print("Balances:", bal.get("balances"))
        except Exception as exc:
            print(f"WARN: accountBalance failed ({exc})")

        for chain_env, label in (
            ("VNX_CELO_BLOCKCHAIN", "CELO"),
            ("VNX_SOL_BLOCKCHAIN", "SOL"),
        ):
            bc = os.getenv(chain_env, label)
            try:
                dep = await vnx.deposit_address("VCHF", bc)
                print(f"Deposit {bc}:", dep.get("address"))
            except Exception as exc:
                print(f"WARN: depositAddress {bc} failed ({exc})")

        try:
            addrs = await vnx.withdraw_addresses()
            print("Withdraw addresses:", addrs.get("addresses"))
        except Exception as exc:
            print(f"WARN: withdrawAddresses failed ({exc})")


if __name__ == "__main__":
    asyncio.run(main())
