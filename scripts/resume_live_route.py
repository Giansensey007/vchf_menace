#!/usr/bin/env python3
"""Resume live route after leg1: sell Celo VCHF → base_to_solana → solana_to_vnx."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.bridge.cctp_queue import CctpClaimQueue
from src.bridge.wormhole_queue import WormholeClaimQueue
from src.config_loader import load_bot_config, load_chains, load_tokens, token_decimals
from src.db import init_db
from src.execution.base import BaseExecutor
from src.execution.executor import ArbExecutor, CycleState
from src.execution.solana import SolanaExecutor
from src.execution.tx_log import log_tx, tx_log_path
from src.quotes.http_client import build_client
from src.quotes.types import to_human
from src.treasury.manager import TreasuryManager
from src.vnx.client import VnxClient


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _audit() -> None:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        _log(
            f"Platform: USDC={vnx.usdc_balance(bal):.2f} VCHF={vnx.vchf_balance(bal):.2f} "
            f"CHF={vnx._asset_balance(bal, 'CHF'):.2f}"
        )
    celo = BaseExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    _log(
        f"Celo: USDT={to_human(celo.balance_erc20(chains['celo'].hub_token), chains['celo'].hub_decimals):.2f} "
        f"VCHF={to_human(celo.balance_erc20(token.chains['celo']), dec):.4f}"
    )
    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    usdc_ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token))
    vchf_ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(token.chains["solana"]))
    usdc = sol.client.get_token_account_balance(usdc_ata).value.ui_amount or 0
    try:
        vchf = sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0
    except Exception:
        vchf = 0.0
    _log(f"Sol: USDC={usdc:.2f} VCHF={vchf:.4f} SOL={sol.balance_lamports()/1e9:.4f}")


async def _cctp_claim() -> None:
    queue = CctpClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(client, interval_sec=30, max_rounds=120, discover_first=True)
    _log(f"CCTP claimed={summary['claimed']} remaining={summary['remaining']}")


async def _wormhole_claim() -> None:
    queue = WormholeClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(client, max_rounds=120)
    _log(f"Wormhole claimed={summary['claimed']} remaining={summary['remaining']}")


async def _platform() -> dict[str, float]:
    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        return {
            "vchf": vnx.vchf_balance(bal),
            "usdc": vnx.usdc_balance(bal),
            "chf": vnx._asset_balance(bal, "CHF"),
        }


async def _celo_vchf() -> float:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    celo = BaseExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    return float(to_human(celo.balance_erc20(token.chains["celo"]), dec))


async def _sell_celo_vchf() -> tuple[bool, str | None]:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    celo = BaseExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    usdt_token = chains["celo"].hub_token
    vchf_raw = celo.balance_erc20(token.chains["celo"])
    if vchf_raw <= 0:
        return True, None
    vchf_ui = float(to_human(vchf_raw, dec))
    _log(f"\n--- Sell {vchf_ui:.4f} VCHF → USDT on Celo ---")
    sim = celo.simulate_swap(token.chains["celo"], usdt_token, vchf_raw, cfg.slippage_bps)
    if not sim:
        _log("FAIL: no Celo sell quote")
        return False, None
    min_usdt = int(sim["amount_out"] * (1 - cfg.slippage_bps / 10000))
    tx = celo.swap_exact_input(token.chains["celo"], usdt_token, vchf_raw, min_usdt)
    if not tx:
        _log("FAIL: Celo sell broadcast")
        return False, None
    log_tx("resume_celo_sell_vchf", "celo", tx)
    _log(f"  OK sell tx={tx}")
    return True, tx


async def run_resume() -> int:
    init_db()
    before = await _platform()
    celo_vchf = await _celo_vchf()
    _log(f"\n========== RESUME ROUTE (legs 2–3) ==========")
    _log(f"Before: platform VCHF={before['vchf']:.2f} Celo VCHF={celo_vchf:.4f}")

    if celo_vchf >= 0.5:
        ok_sell, _ = await _sell_celo_vchf()
        if not ok_sell:
            return 1

    size = max(5.0, await _celo_vchf()) if await _celo_vchf() >= 5.0 else 5.0

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    treasury = TreasuryManager(chains, token, cfg)
    ex = ArbExecutor(chains, token, cfg)
    all_txs: dict[str, list[str]] = {}
    r3 = None

    async with build_client() as client:
        await _wormhole_claim()
        await _cctp_claim()
        await _audit()

        _log(f"\n--- Leg 2: base_to_solana @ {size:.2f} VCHF ---")
        prep2 = await treasury.prepare_for_direction("base_to_solana", size)
        _log(f"  prep: ready={prep2.ready} size={prep2.size_vchf:.2f} notes={prep2.notes}")
        if not prep2.ready:
            _log(f"ABORT leg2: {prep2.notes}")
            return 1
        r2 = await ex.run_cycle(client, "base_to_solana", prep2.size_vchf, force_execute=True)
        all_txs["base_to_solana"] = [t for t in r2.tx_hashes if t and not t.startswith("dry-run")]
        _log(f"  leg2 state={r2.state.value} txs={all_txs['base_to_solana']} err={r2.error}")
        if r2.state != CycleState.DONE:
            return 1

        await _wormhole_claim()
        await _cctp_claim()
        await _audit()

        leg3_size = prep2.size_vchf
        _log(f"\n--- Leg 3: solana_to_vnx @ {leg3_size:.2f} VCHF ---")
        prep3 = await treasury.prepare_for_direction("solana_to_vnx", leg3_size)
        _log(f"  prep: ready={prep3.ready} size={prep3.size_vchf:.2f} notes={prep3.notes}")
        if not prep3.ready:
            _log(f"ABORT leg3: {prep3.notes}")
            return 1
        r3 = await ex.run_cycle(client, "solana_to_vnx", prep3.size_vchf, force_execute=True)
        all_txs["solana_to_vnx"] = [t for t in r3.tx_hashes if t and not t.startswith("dry-run")]
        _log(f"  leg3 state={r3.state.value} txs={all_txs['solana_to_vnx']} err={r3.error}")

    await _wormhole_claim()
    await _cctp_claim()
    await _audit()
    after = await _platform()

    closed = r3 is not None and r3.state == CycleState.DONE
    _log("\n=== RESUME SUMMARY ===")
    _log(f"  platform_vchf before={before['vchf']:.2f} after={after['vchf']:.2f}")
    _log(f"  closed_success={closed}")
    for leg, txs in all_txs.items():
        for tx in txs:
            _log(f"  {leg}: {tx}")

    if tx_log_path().exists():
        _log("\n=== TX log tail ===")
        for line in tx_log_path().read_text(encoding="utf-8").strip().splitlines()[-20:]:
            row = json.loads(line)
            _log(f"  {row.get('intent')} | {row.get('chain')} | {row.get('tx_hash')}")

    return 0 if closed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_resume()))
