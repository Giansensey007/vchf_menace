#!/usr/bin/env python3
"""Live quote verification against screenshot baselines."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import load_chains, load_tokens, token_decimals
from src.quotes.http_client import build_client
from src.quotes.router import buy_token_with_stable, sell_token_for_stable
from src.quotes.types import from_human, to_human
from src.vnx.auth import ensure_public_key_env


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="10,50,100")
    args = p.parse_args()
    sizes = [float(x) for x in args.sizes.split(",")]

    try:
        ensure_public_key_env()
    except Exception:
        pass

    chains = load_chains()
    token = load_tokens()["VCHF"]
    celo = chains["base"]
    sol = chains["solana"]

    async with build_client() as client:
        for size in sizes:
            print(f"\n=== Size {size} VCHF ===")
            sol_dec = token_decimals(token, "solana")
            celo_dec = token_decimals(token, "base")

            vchf_amt = from_human(size, sol_dec)
            sell_sol = await sell_token_for_stable(client, sol, token, "solana", vchf_amt)
            if sell_sol:
                usdc = float(to_human(sell_sol.amount_out, sol.hub_decimals))
                rate = usdc / size if size else 0
                print(f"  Sol sell: {size} VCHF -> {usdc:.4f} USDC ({rate:.4f} USDC/VCHF) via {sell_sol.provider}")
            else:
                print("  Sol sell: FAILED")

            usdt_probe = from_human(size * 1.35, celo.hub_decimals)
            buy_celo = await buy_token_with_stable(client, celo, token, "base", usdt_probe)
            if buy_celo:
                vchf = float(to_human(buy_celo.amount_out, celo_dec))
                usdt = float(to_human(usdt_probe, celo.hub_decimals))
                print(f"  Base buy: {usdt:.2f} USDT -> {vchf:.4f} VCHF via {buy_celo.provider}")
            else:
                print("  Base buy: FAILED")


if __name__ == "__main__":
    asyncio.run(main())
