#!/usr/bin/env python3
"""Fund ~$10 native gas on ETH, SOL, and BASE from platform USDC.

Requires VNX ETH whitelist for 0xF7a991… (VNX_ETH_WITHDRAW_LABEL, e.g. Arb_explorer).
Until VNX confirms the ETH address, withdraw will fail at the API — route stays enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.hub_eth import fund_all_chain_gas
from src.config_loader import load_chains
from src.execution.base import BaseExecutor
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.http_client import build_client
from src.quotes.types import to_human
from src.vnx.client import VnxClient


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _audit() -> None:
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    celo = BaseExecutor(chains["base"])
    sol = SolanaExecutor(chains["solana"])
    async with VnxClient() as vnx:
        plat = await vnx.account_balance()
        usdc = vnx.usdc_balance(plat)
    _log(
        f"Platform USDC={usdc:.2f} | "
        f"ETH native={float(eth.w3.from_wei(eth.balance_native(), 'ether')):.6f} "
        f"USDC={float(to_human(eth.balance_erc20(chains['ethereum'].hub_token), 6)):.2f} | "
        f"SOL native={sol.balance_lamports()/1e9:.4f} | "
        f"BASE native={float(celo.w3.from_wei(celo.balance_native(), 'ether')):.2f}"
    )


async def main() -> int:
    p = argparse.ArgumentParser(description="Fund native gas on ETH/SOL/BASE from VNX USDC")
    p.add_argument("--amount", type=float, default=10.0, help="USDC worth per chain (default 10)")
    p.add_argument("--no-withdraw", action="store_true", help="Skip VNX withdraw (use ETH wallet USDC)")
    args = p.parse_args()

    _log("=== Before ===")
    await _audit()

    async with build_client() as client:
        result = await fund_all_chain_gas(
            client,
            amount_usdc_per_chain=args.amount,
            withdraw_from_vnx=not args.no_withdraw,
        )

    _log("\n=== Result ===")
    _log(json.dumps(result, indent=2, default=str))

    _log("\n=== After ===")
    await _audit()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
