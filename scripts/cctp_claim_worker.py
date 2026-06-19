#!/usr/bin/env python3
"""
CCTP claim worker — discover pending burns, poll Iris, claim on dest chain.

Usage:
  python scripts/cctp_claim_worker.py              # run until queue empty
  python scripts/cctp_claim_worker.py --once       # single process pass
  python scripts/cctp_claim_worker.py --discover   # scan wallets only
  python scripts/cctp_claim_worker.py --interval 15
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.cctp_queue import CctpClaimQueue
from src.quotes.http_client import build_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cctp_worker")


async def main() -> None:
    p = argparse.ArgumentParser(description="CCTP pending claim queue worker")
    p.add_argument("--interval", type=float, default=float(__import__("os").getenv("CCTP_CLAIM_INTERVAL_SEC", "30")))
    p.add_argument("--once", action="store_true", help="Single refresh+claim pass")
    p.add_argument("--discover", action="store_true", help="Discover burns from wallet history only")
    p.add_argument("--max-rounds", type=int, default=120)
    args = p.parse_args()

    queue = CctpClaimQueue()
    async with build_client() as client:
        if args.discover:
            n = await queue.discover(client)
            print(f"Discovered {n} new burn(s); {len(queue._store.pending())} pending total")
            print(json.dumps([i.id for i in queue._store.pending()], indent=2))
            return

        if args.once:
            await queue.discover(client)
            claimed = await queue.process_once(client)
            pending = queue._store.pending()
            print(f"Claimed {claimed}; {len(pending)} still pending")
            for item in pending:
                print(f"  {item.status} {item.source_tx} → domain {item.dest_domain} err={item.error}")
            sys.exit(0 if not pending else 1)

        summary = await queue.run_until_empty(
            client,
            interval_sec=args.interval,
            max_rounds=args.max_rounds,
            discover_first=True,
        )
        print(json.dumps(summary, indent=2))
        sys.exit(0 if summary["remaining"] == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
