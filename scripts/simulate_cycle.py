#!/usr/bin/env python3
"""Simulate all arb routes without broadcasting."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import load_bot_config, load_chains, load_tokens
from src.quotes.http_client import build_client
from src.scanner.routes import ALL_DIRECTIONS
from src.scanner.simulator import simulate_all_routes, simulate_direction


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true", help="Simulate all 6 directions")
    p.add_argument("--both", action="store_true", help="Legacy: celo<->sol only")
    p.add_argument("--direction", choices=list(ALL_DIRECTIONS))
    p.add_argument("--size", type=float, default=50)
    args = p.parse_args()

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()

    if args.size < 40:
        print("Note: VNX routes need size >= 30 VCHF")

    async with build_client() as client:
        if args.all:
            sims = await simulate_all_routes(client, chains, token, cfg, args.size)
        else:
            directions = list(ALL_DIRECTIONS) if not args.both else ["base_to_solana", "solana_to_base"]
            if args.direction:
                directions = [args.direction]
            sims = []
            for d in directions:
                sims.append(await simulate_direction(client, chains, token, cfg, d, args.size))

        for sim in sims:
            print(f"\n=== {sim.direction} ({sim.buy_chain} -> {sim.sell_chain}) @ {args.size} VCHF ===")
            print(f"  bridge:     {sim.needs_bridge}")
            print(f"  stable_in:  ${sim.stable_in_usd:.2f}")
            print(f"  stable_out: ${sim.stable_out_usd:.2f}")
            print(f"  fees:       ${sim.fees_usd:.2f}")
            print(f"  net:        ${sim.net_profit_usd:.2f}")
            print(f"  sanity:     {sim.sanity_ok} {sim.sanity_notes}")
            print(f"  profitable: {sim.profitable}")
            if sim.error:
                print(f"  error:      {sim.error}")


if __name__ == "__main__":
    asyncio.run(main())
