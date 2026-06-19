#!/usr/bin/env python3
"""Poll Wormhole queue and claim VAAs on destination chains."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.wormhole_queue import WormholeClaimQueue
from src.quotes.http_client import build_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=float(os.getenv("WORMHOLE_CLAIM_INTERVAL_SEC", "30")))
    p.add_argument("--rounds", type=int, default=int(os.getenv("WORMHOLE_CLAIM_MAX_ROUNDS", "120")))
    args = p.parse_args()
    queue = WormholeClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(client, interval_sec=args.interval, max_rounds=args.rounds)
    print(f"Wormhole claimed={summary['claimed']} remaining={summary['remaining']} rounds={summary['rounds']}")
    sys.exit(0 if summary["remaining"] == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
