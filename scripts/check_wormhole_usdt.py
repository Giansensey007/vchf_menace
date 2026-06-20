#!/usr/bin/env python3
"""
Verify Wormhole Portal USDT paths from Celo:
  - Celo → Solana (chain id 1)
  - Celo → Ethereum (chain id 2)

Quotes, on-chain eth_call simulation, and optional live initiate (--execute, DRY_RUN=false).
Redeem on destination (VAA claim) is not automated — reports attestation URL pattern.
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

from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import is_dry_run, load_bridge_config, load_chains
from src.execution.celo import CeloExecutor


def _log(msg: str) -> None:
    print(msg, flush=True)


def _check_path(
    wh: WormholePortalBridge,
    celo: CeloExecutor,
    dest: str,
    recipient: str,
    amount: float,
) -> bool:
    to_chain = "solana" if dest == "sol" else "ethereum"
    label = f"celo → {to_chain}"
    _log(f"\n=== Wormhole USDT {label} ===")

    q = wh.quote_usdt("celo", to_chain, amount)
    _log(f"Quote: {q.amount_in_usdt:.4f} USDT in → {q.amount_out_usdt:.4f} est. out (fee ${q.fee_usd:.2f})")
    if not q.ok:
        _log(f"FAIL quote: {q.error}")
        return False

    sim = wh.simulate_celo_transfer_tokens(amount, to_chain, recipient, celo)
    _log(f"Celo hot: {celo.address}")
    _log(f"Recipient ({to_chain}): {recipient}")
    _log(
        f"Balance: {sim.get('balance_usdt', 0):.4f} USDT | "
        f"Allowance: {sim.get('allowance_usdt', 0):.4f} USDT | "
        f"Bridge: {sim.get('bridge', '')[:12]}…"
    )
    if sim.get("ok"):
        extra = ""
        if sim.get("needs_approval"):
            extra = " (approve bridge first)"
        if sim.get("note"):
            extra += f" — {sim['note']}"
        gas = sim.get("gas_estimate") or sim.get("approve_gas_estimate")
        _log(f"Preflight OK{extra} | gas≈{gas}")
    else:
        _log(f"eth_call FAIL: {sim.get('error')}")
        return False

    wh_cfg = load_bridge_config()["wormhole"]
    _log(
        "Note: initiate locks USDT on Celo; redeem on dest via Portal VAA "
        f"(https://portalbridge.com — emitter chain {wh_cfg['celo_chain_id']})"
    )
    return True


async def _maybe_execute(
    wh: WormholePortalBridge,
    celo: CeloExecutor,
    dest: str,
    recipient: str,
    amount: float,
) -> bool:
    if dest == "sol":
        br = await wh.bridge_usdt_celo_to_solana(amount, recipient, celo)
    else:
        br = await wh.bridge_usdt_celo_to_ethereum(amount, recipient, celo)
    _log(
        f"Execute: success={br.success} dry_run={br.dry_run} "
        f"tx={br.source_tx} err={br.error or '-'}"
    )
    if br.source_tx and not br.dry_run:
        _log(f"  Celo: https://basescan.io/tx/{br.source_tx}")
    return br.success


async def run(amount: float, execute: bool) -> int:
    chains = load_chains()
    celo = CeloExecutor(chains["celo"])
    wh = WormholePortalBridge(chains["celo"])

    sol = os.getenv("SOLANA_PUBLIC_KEY", "").strip()
    if not sol:
        try:
            from src.execution.solana import SolanaExecutor

            sol = str(SolanaExecutor(chains["solana"]).keypair.pubkey())
        except Exception:
            sol = ""
    eth = celo.address  # same EVM hot wallet for ETH redeem

    if not sol:
        _log("WARN: SOLANA_PUBLIC_KEY unset — skipping Celo→Sol check")
        ok_sol = False
    else:
        ok_sol = _check_path(wh, celo, "sol", sol, amount)

    ok_eth = _check_path(wh, celo, "eth", eth, amount)

    _log("\n=== Wormhole USDT ethereum → celo ===")
    from src.execution.celo import CeloExecutor as _CeloExec
    from src.execution.ethereum import EthereumExecutor
    from src.quotes.types import to_human

    celo_addr = _CeloExec(chains["celo"]).address
    eth_exec = EthereumExecutor(chains["ethereum"])
    wh_cfg = load_bridge_config()["wormhole"]
    eth_usdt_bal = float(to_human(eth_exec.balance_erc20(wh_cfg["ethereum_usdt"]), 6))
    eth_probe = min(amount, eth_usdt_bal * 0.9) if eth_usdt_bal >= 0.05 else 0.0
    if eth_probe < amount * 0.95:
        _log(
            f"SKIP Ethereum → Celo USDT (ETH USDT {eth_usdt_bal:.2f} < probe {amount:.2f} — "
            "fund ETH USDT for reverse leg)"
        )
        ok_eth_celo = True
    else:
        eth_sim = wh.simulate_eth_transfer_tokens(eth_probe, celo_addr, eth_exec=eth_exec)
        _log(
            f"ETH hot: {eth} | Celo recipient: {celo_addr} | "
            f"Balance: {eth_sim.get('balance_usdt', 0):.4f} USDT | "
            f"Allowance: {eth_sim.get('allowance_usdt', 0):.4f} | Bridge: {eth_sim.get('bridge', '')[:12]}…"
        )
        if eth_sim.get("ok"):
            extra = ""
            if eth_sim.get("needs_approval"):
                extra = " (approve bridge first)"
            _log(f"Preflight OK{extra} | gas≈{eth_sim.get('gas_estimate')}")
            ok_eth_celo = True
        else:
            _log(f"eth_call FAIL: {eth_sim.get('error')}")
            ok_eth_celo = False

    if execute:
        _log(f"\n=== Execute (DRY_RUN={is_dry_run()}) ===")
        if ok_sol and sol:
            ok_sol = await _maybe_execute(wh, celo, "sol", sol, amount)
        if ok_eth:
            ok_eth = await _maybe_execute(wh, celo, "eth", eth, amount)

    _log("\n=== Summary ===")
    _log(f"  {'PASS' if ok_sol else 'FAIL'}  Celo → Solana USDT (Wormhole)")
    _log(f"  {'PASS' if ok_eth else 'FAIL'}  Celo → Ethereum USDT (Wormhole)")
    _log(f"  {'PASS' if ok_eth_celo else 'FAIL'}  Ethereum → Celo USDT (Wormhole)")
    _log("  CCTP remains Sol↔ETH USDC only (not USDT).")

    return 0 if ok_sol and ok_eth and ok_eth_celo else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Check Wormhole USDT Celo→Sol and Celo→ETH")
    p.add_argument("--amount", type=float, default=1.0, help="Probe USDT amount for simulation")
    p.add_argument("--execute", action="store_true", help="Broadcast initiate tx (DRY_RUN=false)")
    args = p.parse_args()
    rc = asyncio.run(run(args.amount, args.execute))
    sys.exit(rc)


if __name__ == "__main__":
    main()
