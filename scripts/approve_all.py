#!/usr/bin/env python3
"""One-time infinite ERC20 approvals for all swap routers, bridges, and aggregators."""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from src.execution.token_approvals import ensure_infinite_approvals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)


def main() -> None:
    results = ensure_infinite_approvals()
    ok = sum(1 for r in results if r.get("tx"))
    print(f"Done: {ok}/{len(results)} approval steps completed")
    for r in results:
        print(f"  [{r.get('chain')}] {r.get('label')}: {r.get('tx')}")


if __name__ == "__main__":
    main()
