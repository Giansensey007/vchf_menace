#!/usr/bin/env python3
"""Quote or dry-run Wormhole Portal USDT bridge between Celo and Solana."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import load_chains


def main() -> None:
    p = argparse.ArgumentParser(description="Wormhole Portal USDT bridge (Celo ↔ Solana)")
    p.add_argument("direction", choices=["celo_to_solana", "solana_to_celo", "celo_to_ethereum"])
    p.add_argument("amount", type=float, help="USDT amount")
    p.add_argument("--execute", action="store_true", help="Send tx (requires DRY_RUN=false)")
    args = p.parse_args()

    chains = load_chains()
    celo = chains["celo"]
    wh = WormholePortalBridge(celo)

    from_chain, to_chain = {
        "celo_to_solana": ("celo", "solana"),
        "solana_to_celo": ("solana", "celo"),
        "celo_to_ethereum": ("celo", "ethereum"),
    }[args.direction]
    q = wh.quote_usdt(from_chain, to_chain, args.amount)
    print(f"Provider: {q.provider}")
    print(f"In:  {q.amount_in_usdt:.4f} USDT")
    print(f"Out: {q.amount_out_usdt:.4f} USDT (est.)")
    print(f"Fee: ${q.fee_usd:.2f}")

    if not args.execute:
        print("Quote only. Pass --execute to bridge (respects DRY_RUN).")
        return

    async def run() -> None:
        import os

        if args.direction == "celo_to_solana":
            sol = os.getenv("SOLANA_PUBLIC_KEY", "")
            if not sol:
                print("Set SOLANA_PUBLIC_KEY in .env")
                sys.exit(1)
            from src.execution.celo import CeloExecutor

            br = await wh.bridge_usdt_celo_to_solana(args.amount, sol, CeloExecutor(celo))
        elif args.direction == "celo_to_ethereum":
            from src.execution.celo import CeloExecutor

            exec_ = CeloExecutor(celo)
            br = await wh.bridge_usdt_celo_to_ethereum(args.amount, exec_.address, exec_)
        else:
            from src.execution.celo import CeloExecutor

            br = await wh.bridge_usdt_solana_to_celo(args.amount, CeloExecutor(celo).address)
        print(f"Success={br.success} dry_run={br.dry_run} tx={br.source_tx} err={br.error}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
