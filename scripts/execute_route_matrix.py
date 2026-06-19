#!/usr/bin/env python3
"""
Force-execute route legs at TEST_VCHF (default 31) without profit gate.
Logs every TX with intent + explorer URL to data/tx_log.jsonl.

Usage:
  python scripts/execute_route_matrix.py --step audit
  python scripts/execute_route_matrix.py --step cctp-claim
  python scripts/execute_route_matrix.py --step production
  python scripts/execute_route_matrix.py --step all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.vnx.deposits import check_usdc_deposit_amount, min_deposit_usdc
from src.bridge.cctp_queue import CctpClaimQueue
from src.bridge.hub_eth import (
    celo_usdt_to_sol_usdc,
    celo_usdt_to_vnx_usdc,
    eth_usdc_to_vnx,
    sol_usdc_to_celo_usdt,
    vnx_usdc_to_eth,
    wormhole_celo_to_eth,
    wormhole_celo_to_sol_direct,
    wormhole_eth_to_celo,
    wormhole_eth_to_celo_via_usdc,
    eth_usdt_to_sol_usdc,
)
from src.bridge.wormhole_queue import WormholeClaimQueue
from src.config_loader import load_bot_config, load_chains, load_tokens, token_decimals
from src.execution.celo import CeloExecutor
from src.execution.executor import ArbExecutor, CycleRecord, CycleState
from src.execution.solana import SolanaExecutor
from src.execution.tx_log import TX_LOG_PATH, log_platform_order, log_tx
from src.quotes.http_client import build_client
from src.quotes.types import from_human, to_human
from src.scanner.routes import active_directions
from src.scanner.simulator import simulate_direction
from src.vnx.client import VnxClient
from src.vnx.trading import platform_buy_vchf, platform_sell_vchf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("route_matrix")

import os

TEST_VCHF = 31.0
_ROUTE_SIZE = TEST_VCHF  # overridden by --size CLI flag
PROBE_VCHF = 5.0  # matches VNX_MIN_DEPOSIT_VCHF_CELO for Celo deposit routes
PROBE_USDC = 0.4  # minimum Sol USDC for DEX probe when balance < 5
CELO_MIN_VCHF = 5.0  # VNX platform min cumulative deposit on CELO
CCTP_USDC = 5.0
ETH_MIN_USDC_DEPOSIT = min_deposit_usdc("ETH")  # VNX cumulative credit min on ETH (default 20)
HUB_USDC = ETH_MIN_USDC_DEPOSIT  # never deposit ETH USDC to VNX below this
HUB_USDT = 5.0  # wormhole hub probes (separate from VNX USDC minimum)
CCTP_CLAIM_INTERVAL = float(os.getenv("CCTP_CLAIM_INTERVAL_SEC", "30"))
CCTP_CLAIM_MAX_ROUNDS = int(os.getenv("CCTP_CLAIM_MAX_ROUNDS", "120"))
_cctp_discovered = False
PRODUCTION_ROUTE_ORDER = (
    "vnx_to_solana",
    "solana_to_vnx",
    "solana_to_celo",
    "celo_to_solana",
)


def _log(msg: str) -> None:
    print(msg, flush=True)


async def audit() -> None:
    from src.treasury.in_flight import InFlightLedger
    from src.treasury.manager import TreasuryManager

    chains = load_chains()
    token = load_tokens()["VCHF"]
    bot_cfg = load_bot_config()
    treasury = TreasuryManager(chains, token, bot_cfg)
    snap = await treasury.snapshot()
    _log(treasury.balance_line(snap))
    _log(InFlightLedger("VCHF").format_audit_block())
    celo = CeloExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    from src.bridge.celo_usdt import celo_usdt_balances

    celo_bals = celo_usdt_balances(celo)
    celo_line = (
        f"Celo: USDT={celo_bals['canonical']:.2f} "
        f"VCHF={to_human(celo.balance_erc20(token.chains['celo']), dec):.4f}"
    )
    if celo_bals["wrapped_eth"] >= 0.01:
        celo_line += f" (wrapped ETH-USDT={celo_bals['wrapped_eth']:.2f} — run consolidate-celo-usdt)"
    _log(celo_line)
    sol = SolanaExecutor(chains["solana"])
    sdec = token_decimals(token, "solana")
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    usdc_ata = get_associated_token_address(
        sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token)
    )
    vchf_ata = get_associated_token_address(
        sol.keypair.pubkey(), Pubkey.from_string(token.chains["solana"])
    )
    usdc = sol.client.get_token_account_balance(usdc_ata).value.ui_amount or 0
    try:
        vchf = sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0
    except Exception:
        vchf = 0.0
    _log(f"Sol: USDC={usdc:.2f} VCHF={vchf:.4f} SOL={sol.balance_lamports()/1e9:.4f}")
    try:
        from src.execution.ethereum import EthereumExecutor
        from src.config_loader import load_bridge_config

        wh = load_bridge_config()["wormhole"]
        eth = EthereumExecutor(chains["ethereum"])
        _log(
            f"ETH: USDC={to_human(eth.balance_erc20(chains['ethereum'].hub_token), 6):.2f} "
            f"USDT={to_human(eth.balance_erc20(wh['ethereum_usdt']), 6):.2f} "
            f"ETH={eth.balance_native()/1e18:.4f}"
        )
        wrapped = wh.get("celo_usdt_wormhole_from_eth")
        if wrapped:
            from src.execution.ethereum import ERC20_ABI
            from web3 import Web3

            wbal = celo.w3.eth.contract(
                address=Web3.to_checksum_address(wrapped), abi=ERC20_ABI
            ).functions.balanceOf(celo.address).call()
            if wbal > 10_000:
                _log(f"Celo wrapped ETH-USDT: {to_human(wbal, 6):.2f} (Wormhole redeem)")
    except Exception as exc:
        _log(f"ETH: unavailable ({exc})")


async def step_wormhole_usdt_check(amount: float = 1.0) -> bool:
    """On-chain verify Wormhole USDT paths (eth_call, no broadcast)."""
    from scripts.check_wormhole_usdt import run as wormhole_check

    _log(f"\n=== Wormhole USDT bridge check ({amount} USDT probe) ===")
    rc = await wormhole_check(amount, execute=False)
    return rc == 0


async def step_wormhole_preflight() -> bool:
    """Wormhole sim: require ETH→Celo when funded; Celo outbound when funded."""
    from scripts.check_wormhole_usdt import run as wormhole_check
    from src.bridge.wormhole import WormholePortalBridge
    from src.config_loader import load_bridge_config, load_chains
    from src.execution.celo import CeloExecutor
    from src.execution.ethereum import EthereumExecutor
    from src.quotes.types import to_human

    chains = load_chains()
    wh_cfg = load_bridge_config()["wormhole"]
    celo = CeloExecutor(chains["celo"])
    eth = EthereumExecutor(chains["ethereum"])
    wh = WormholePortalBridge(chains["celo"])

    eth_usdt = float(to_human(eth.balance_erc20(wh_cfg["ethereum_usdt"]), 6))
    celo_usdt = float(to_human(celo.balance_erc20(chains["celo"].hub_token), 6))
    probe = min(1.0, eth_usdt * 0.9) if eth_usdt >= 0.05 else 0.0

    if probe >= 0.05:
        eth_ok = wh.simulate_eth_transfer_tokens(probe, celo.address, eth_exec=eth).get("ok")
        _log(f"\n=== Wormhole preflight ETH→Celo (${probe:.2f} USDT): {'OK' if eth_ok else 'FAIL'} ===")
        if not eth_ok:
            return False
    else:
        _log(f"\n=== Wormhole preflight ETH→Celo: SKIP (ETH USDT {eth_usdt:.2f} — sim when funded) ===")

    if celo_usdt >= 0.05:
        celo_probe = min(1.0, celo_usdt * 0.9)
        rc = await wormhole_check(celo_probe, execute=False)
        _log(f"=== Wormhole preflight Celo outbound (${celo_probe:.2f} USDT): {'OK' if rc == 0 else 'FAIL'} ===")
        if rc != 0:
            _log("  (Celo outbound sim failed — may need more canonical USDT or CELO gas)")
            return False
        return True
    _log(f"SKIP Celo→* sim (canonical USDT {celo_usdt:.2f} < 0.05 — fund Celo for outbound)")
    return True


async def step_cctp_claim(*, discover: bool | None = None) -> bool:
    global _cctp_discovered
    if discover is None:
        discover = not _cctp_discovered
    _log("\n=== CCTP claim queue (discover + claim until empty) ===")
    queue = CctpClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(
            client,
            interval_sec=CCTP_CLAIM_INTERVAL,
            max_rounds=CCTP_CLAIM_MAX_ROUNDS,
            discover_first=discover,
        )
    if discover:
        _cctp_discovered = True
    _log(f"CCTP claimed={summary['claimed']} remaining={summary['remaining']} rounds={summary['rounds']}")
    return summary["remaining"] == 0


async def step_rebalance(execute: bool = True) -> bool:
    from scripts.rebalance_for_test import rebalance

    return await rebalance(execute)


async def step_platform_buy() -> bool:
    _log("\n=== Platform buy VCHF ===")
    cfg = load_bot_config()
    async with VnxClient() as vnx:
        buy = await platform_buy_vchf(cfg, TEST_VCHF, vnx=vnx)
        if not buy.success:
            _log(f"FAIL: {buy.error}")
            return False
        log_platform_order("platform_buy_vchf", buy.ordid, qty=buy.quantity, price=buy.price)
        _log(f"OK ordid={buy.ordid} qty={buy.quantity} price={buy.price}")
    return True


async def step_platform_sell() -> bool:
    _log("\n=== Platform sell VCHF ===")
    cfg = load_bot_config()
    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        qty = min(TEST_VCHF, vnx.vchf_balance(bal))
        if qty < TEST_VCHF * 0.99:
            _log(f"FAIL: only {qty:.2f} VCHF on platform")
            return False
        sell = await platform_sell_vchf(cfg, qty, vnx=vnx)
        if not sell.success:
            _log(f"FAIL: {sell.error}")
            return False
        log_platform_order("platform_sell_vchf", sell.ordid, sold=sell.sold, currency=sell.sold_currency)
        _log(f"OK ordid={sell.ordid} sold={sell.sold} {sell.sold_currency}")
    return True


def _log_cycle_txs(direction: str, record: CycleRecord) -> None:
    for tx in record.tx_hashes:
        if not tx or tx.startswith("dry-run"):
            continue
        if tx.isdigit() or tx.startswith("ordid:"):
            continue
        if len(tx) > 60 or (len(tx) > 40 and not tx.startswith("0x")):
            chain = "solana"
        elif tx.startswith("0x"):
            chain = "celo" if "celo" in direction and "vnx" not in direction else "ethereum"
        else:
            chain = "solana" if "solana" in direction else "celo"
        log_tx(f"route_{direction}", chain, tx, ok=record.state == CycleState.DONE)


async def _force_exec(direction: str, size: float = TEST_VCHF) -> bool:
    from src.treasury.manager import TreasuryManager
    from src.treasury.loops import origin_for_direction
    from src.vnx.deposits import check_deposit_amount

    if direction in ("celo_to_solana", "celo_to_vnx"):
        import os

        bc = os.getenv("VNX_CELO_BLOCKCHAIN", "CELO")
        err = check_deposit_amount(bc, size)
        if err:
            _log(f"  SKIP {direction}: {err}")
            return False
    if direction in ("solana_to_vnx", "solana_to_celo"):
        import os

        bc = os.getenv("VNX_SOL_BLOCKCHAIN", "SOL")
        err = check_deposit_amount(bc, size)
        if err:
            _log(f"  SKIP {direction}: {err}")
            return False

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    ex = ArbExecutor(chains, token, cfg)
    treasury = TreasuryManager(chains, token, cfg)
    origin = origin_for_direction(direction)

    async with build_client() as client:
        from src.scanner.simulator import simulate_round_trip

        rt = await simulate_round_trip(client, chains, token, cfg, direction, size, origin=origin)
        _log(
            f"  round-trip {direction}@{origin}: primary=${rt.primary.net_profit_usd:.2f} "
            f"return={rt.return_direction} ${rt.return_sim.net_profit_usd if rt.return_sim else 0:.2f} "
            f"total=${rt.round_trip_profit_usd:.2f}"
        )

        result = await treasury.run_closed_loop(
            client,
            ex,
            origin=origin,
            direction=direction,
            size_vchf=size,
            force_return=True,
            force_execute=True,
        )
        _log(
            f"  {'OK' if result.closed else 'FAIL'} closed={result.closed} "
            f"primary={result.primary_direction} return={result.return_direction} "
            f"reason={result.reason}"
        )
        if result.primary:
            _log_cycle_txs(direction, result.primary)
        if result.return_leg and result.return_direction:
            _log_cycle_txs(result.return_direction, result.return_leg)

        if direction in ("vnx_to_solana", "solana_to_vnx", "celo_to_solana", "solana_to_celo"):
            await step_cctp_claim()

        return result.closed


async def step_celo_swaps() -> bool:
    _log("\n=== Celo buy/sell probe ===")
    chains = load_chains()
    token = load_tokens()["VCHF"]
    celo = CeloExecutor(chains["celo"])
    dec = token_decimals(token, "celo")
    usdt_token = chains["celo"].hub_token
    usdt_bal = float(to_human(celo.balance_erc20(usdt_token), chains["celo"].hub_decimals))
    vchf_raw = celo.balance_erc20(token.chains["celo"])

    # Prefer USDT→VCHF→USDT when USDT funded; else round-trip existing VCHF
    if usdt_bal >= PROBE_USDC:
        usdt_in = from_human(min(5.0, usdt_bal * 0.9), chains["celo"].hub_decimals)
        sim = celo.simulate_swap(usdt_token, token.chains["celo"], usdt_in, 100)
        if not sim:
            _log("FAIL celo buy quote")
            return False
        min_out = int(sim["amount_out"] * 0.97)
        tx1 = celo.swap_exact_input(usdt_token, token.chains["celo"], usdt_in, min_out)
        if not tx1:
            _log("FAIL celo buy")
            return False
        log_tx("probe_celo_buy_vchf", "celo", tx1)
        vchf_raw = celo.balance_erc20(token.chains["celo"])
    elif vchf_raw > 0:
        _log(f"  USDT low ({usdt_bal:.2f}) — round-trip {float(to_human(vchf_raw, dec)):.4f} VCHF")
    else:
        _log(f"FAIL celo swaps — no USDT ({usdt_bal:.2f}) or VCHF on Celo")
        return False

    sell_sim = celo.simulate_swap(token.chains["celo"], usdt_token, vchf_raw, 100)
    min_usdt = int(sell_sim["amount_out"] * 0.97) if sell_sim else int(0.01 * 10**chains["celo"].hub_decimals)
    tx2 = celo.swap_exact_input(token.chains["celo"], usdt_token, vchf_raw, min_usdt)
    if not tx2:
        _log("FAIL celo sell")
        return False
    log_tx("probe_celo_sell_vchf", "celo", tx2)

    if usdt_bal >= PROBE_USDC:
        return True
    # VCHF-only round trip: buy back with USDT received
    usdt_after = celo.balance_erc20(usdt_token)
    if usdt_after <= 0:
        return True
    buy_sim = celo.simulate_swap(usdt_token, token.chains["celo"], usdt_after, 100)
    if not buy_sim:
        return True
    min_vchf = int(buy_sim["amount_out"] * 0.97)
    tx3 = celo.swap_exact_input(usdt_token, token.chains["celo"], usdt_after, min_vchf)
    if tx3:
        log_tx("probe_celo_buy_vchf", "celo", tx3)
    return bool(tx3)


async def step_sol_swaps() -> bool:
    _log("\n=== Sol buy/sell probe ===")
    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    usdc_ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token))
    usdc_bal = float(sol.client.get_token_account_balance(usdc_ata).value.ui_amount or 0)
    vchf_ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(token.chains["solana"]))
    try:
        vchf_bal = float(sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0)
    except Exception:
        vchf_bal = 0.0
    usdc_probe = min(5.0, usdc_bal * 0.85)

    async with build_client() as client:
        if usdc_probe >= PROBE_USDC:
            usdc = from_human(usdc_probe, chains["solana"].hub_decimals)
            tx1 = await sol.swap(client, chains["solana"].hub_token, token.chains["solana"], usdc, cfg.slippage_bps)
            if not tx1:
                _log("FAIL sol buy")
                return False
            log_tx("probe_sol_buy_vchf", "solana", tx1)
            dec = token_decimals(token, "solana")
            vchf_ui = 0.0
            for _ in range(12):
                await asyncio.sleep(2.0)
                try:
                    vchf_ui = float(sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0)
                except Exception:
                    vchf_ui = 0.0
                if vchf_ui >= 0.01:
                    break
            vchf_probe = min(PROBE_VCHF, vchf_ui * 0.95)
        elif vchf_bal >= 0.01:
            _log(f"  USDC low ({usdc_bal:.2f}) — round-trip {vchf_bal:.4f} VCHF")
            dec = token_decimals(token, "solana")
            vchf_probe = min(PROBE_VCHF, vchf_bal * 0.95)
        else:
            _log(f"FAIL sol swaps — USDC {usdc_bal:.2f} and VCHF {vchf_bal:.4f}")
            return False

        if vchf_probe < 0.01:
            _log("FAIL sol sell — no VCHF received")
            return False
        tx2 = await sol.swap(
            client, token.chains["solana"], chains["solana"].hub_token, from_human(vchf_probe, dec), cfg.slippage_bps
        )
        if not tx2:
            _log("FAIL sol sell")
            return False
        log_tx("probe_sol_sell_vchf", "solana", tx2)
    return True


async def step_consolidate_celo_usdt() -> bool:
    """Swap Wormhole wrapped ETH-USDT → canonical Celo USDT (hub token for all routes)."""
    from src.bridge.celo_usdt import celo_usdt_balances, consolidate_wrapped_to_canonical

    before = celo_usdt_balances()
    _log(f"\n=== Celo USDT consolidate (wrapped→canonical) before: {before} ===")
    if before["wrapped_eth"] < 0.01:
        _log("  SKIP — no wrapped USDT")
        return True
    r = consolidate_wrapped_to_canonical()
    after = celo_usdt_balances()
    _log(f"  {'OK' if r['success'] else 'FAIL'} tx={r.get('tx')} after: {after} err={r.get('error')}")
    return r["success"]


async def step_wormhole_claim(*, max_rounds: int = 120) -> bool:
    _log("\n=== Wormhole claim queue ===")
    queue = WormholeClaimQueue()
    async with build_client() as client:
        summary = await queue.run_until_empty(client, max_rounds=max_rounds)
    _log(f"Wormhole claimed={summary['claimed']} remaining={summary['remaining']}")
    return summary["remaining"] == 0


async def step_eth_to_vnx() -> bool:
    dep_err = check_usdc_deposit_amount("ETH", HUB_USDC)
    if dep_err:
        _log(f"\n=== ETH USDC → VNX — SKIP: {dep_err} ===")
        return False
    _log(f"\n=== ETH USDC → VNX platform ${HUB_USDC} ===")
    async with build_client() as client:
        r = await eth_usdc_to_vnx(client, HUB_USDC)
        _log(f"  {'OK' if r['success'] else 'FAIL'} deposit={r.get('deposit_tx')} err={r.get('error')}")
        return r["success"]


async def step_vnx_to_eth() -> bool:
    _log(f"\n=== VNX platform USDC → ETH ${HUB_USDC} ===")
    async with build_client() as client:
        r = await vnx_usdc_to_eth(client, HUB_USDC)
        _log(f"  {'OK' if r['success'] else 'FAIL'} txids={r.get('withdraw_txids')} err={r.get('error')}")
        return r["success"]


async def step_wormhole_celo_to_eth() -> bool:
    amount = await _hub_usdt_amount()
    if amount < 0.05:
        _log(f"\n=== Wormhole CELO→ETH — SKIP (canonical USDT < 0.05) ===")
        return False
    _log(f"\n=== Wormhole CELO→ETH ${amount:.2f} USDT (initiate+redeem) ===")
    async with build_client() as client:
        r = await wormhole_celo_to_eth(client, amount)
        br = r.get("wormhole")
        _log(
            f"  {'OK' if r['success'] else 'FAIL'} src={getattr(br, 'source_tx', None)} "
            f"dst={getattr(br, 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_wormhole_claim()
        return r["success"]


async def step_eth_usdt_to_sol() -> bool:
    """Complete CELO→ETH→SOL: swap ETH USDT → USDC → CCTP → Sol."""
    from scripts.rebalance_for_test import _balances
    from src.config_loader import load_bridge_config

    wh = load_bridge_config()["wormhole"]
    from src.execution.ethereum import EthereumExecutor
    from src.config_loader import load_chains
    from src.quotes.types import to_human

    eth = EthereumExecutor(load_chains()["ethereum"])
    usdt_bal = float(to_human(eth.balance_erc20(wh["ethereum_usdt"]), 6))
    amount = min(1.5, usdt_bal * 0.9)
    if amount < 0.5:
        _log(f"\n=== ETH USDT → SOL — SKIP (ETH USDT {usdt_bal:.2f} < 0.5) ===")
        return False
    _log(f"\n=== Complete CELO→ETH→SOL: ETH USDT→USDC→CCTP→Sol (${amount:.2f}) ===")
    async with build_client() as client:
        r = await eth_usdt_to_sol_usdc(client, amount)
        _log(
            f"  {'OK' if r['success'] else 'PARTIAL/FAIL'} stage={r.get('stage')} "
            f"swap={r.get('swap_tx')} cctp={getattr(r.get('cctp'), 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_cctp_claim()
        return r["success"] or getattr(r.get("cctp"), "source_tx", None) is not None


async def step_wormhole_eth_to_celo() -> bool:
    from src.config_loader import load_bridge_config, load_chains
    from src.execution.ethereum import EthereumExecutor
    from src.quotes.types import to_human

    wh = load_bridge_config()["wormhole"]
    eth = EthereumExecutor(load_chains()["ethereum"])
    usdt_bal = float(to_human(eth.balance_erc20(wh["ethereum_usdt"]), 6))
    amount = min(HUB_USDT, usdt_bal * 0.85)
    if amount < PROBE_USDC:
        _log(f"\n=== Wormhole ETH→CELO — SKIP (ETH USDT {usdt_bal:.2f} < {PROBE_USDC}) ===")
        return False
    _log(f"\n=== Wormhole ETH→CELO ${amount:.2f} USDT (initiate+redeem) ===")
    async with build_client() as client:
        r = await wormhole_eth_to_celo(client, amount)
        br = r.get("wormhole")
        _log(
            f"  {'OK' if r['success'] else 'FAIL'} src={getattr(br, 'source_tx', None)} "
            f"dst={getattr(br, 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_wormhole_claim(max_rounds=60)
        return r["success"]


async def step_celo_usdt_to_vnx() -> bool:
    amount = HUB_USDC  # must meet VNX ETH USDC cumulative minimum after wormhole+swap
    dep_err = check_usdc_deposit_amount("ETH", amount * 0.99)
    if dep_err:
        _log(f"\n=== CELO USDT → VNX — SKIP (expected USDC ~{amount * 0.99:.2f}): {dep_err} ===")
        return False
    _log(f"\n=== CELO USDT → ETH USDC → VNX ${amount} ===")
    async with build_client() as client:
        r = await celo_usdt_to_vnx_usdc(client, amount)
        _log(f"  {'OK' if r['success'] else 'FAIL'} stage={r.get('stage')} err={r.get('error')}")
        if not r["success"]:
            await step_wormhole_claim()
        return r["success"]


async def _hub_usdt_amount() -> float:
    from scripts.rebalance_for_test import _balances

    b = await _balances()
    avail = b.get("celo_usdt", 0) * 0.9
    if avail < 0.05:
        return 0.0
    return min(HUB_USDT, avail)


async def _hub_usdc_amount() -> float:
    from scripts.rebalance_for_test import _balances

    b = await _balances()
    avail = b.get("sol_usdc", 0) * 0.9
    if avail < PROBE_USDC:
        return 0.0
    return min(CCTP_USDC, avail)


async def step_hub_celo_eth_sol() -> bool:
    """CELO USDT → Wormhole → ETH USDT → swap USDC → CCTP → Sol USDC."""
    amount = await _hub_usdt_amount()
    if amount < PROBE_USDC:
        _log(f"\n=== Hub triangle CELO → ETH → SOL — SKIP (Celo USDT < {PROBE_USDC}) ===")
        return False
    _log(f"\n=== Hub triangle CELO → ETH → SOL (${amount:.2f} USDT) ===")
    async with build_client() as client:
        r = await celo_usdt_to_sol_usdc(client, amount)
        _log(
            f"  {'OK' if r['success'] else 'PARTIAL/FAIL'} stage={r.get('stage')} "
            f"wh={getattr(r.get('wormhole'), 'source_tx', None)} "
            f"cctp={getattr(r.get('cctp'), 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_wormhole_claim()
            await step_cctp_claim()
        return r["success"] or getattr(r.get("cctp"), "source_tx", None) is not None


async def step_hub_sol_eth_celo() -> bool:
    """Sol USDC → CCTP → ETH USDC → swap USDT → Wormhole → CELO USDT."""
    amount = await _hub_usdc_amount()
    if amount < PROBE_USDC:
        _log(f"\n=== Hub triangle SOL → ETH → CELO — SKIP (Sol USDC < {PROBE_USDC}) ===")
        return False
    _log(f"\n=== Hub triangle SOL → ETH → CELO (${amount:.2f} USDC) ===")
    async with build_client() as client:
        r = await sol_usdc_to_celo_usdt(client, amount)
        _log(
            f"  {'OK' if r['success'] else 'PARTIAL/FAIL'} stage={r.get('stage')} "
            f"cctp={getattr(r.get('cctp'), 'source_tx', None)} "
            f"wh={getattr(r.get('wormhole'), 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            try:
                await step_cctp_claim()
            except Exception as exc:
                _log(f"  CCTP claim after partial: {exc}")
            try:
                await step_wormhole_claim(max_rounds=40)
            except Exception as exc:
                _log(f"  Wormhole claim after partial: {exc}")
        return r["success"] or getattr(r.get("cctp"), "source_tx", None) is not None


async def step_wormhole_celo_to_sol() -> bool:
    _log(f"\n=== Wormhole CELO→SOL direct ${HUB_USDT} USDT ===")
    async with build_client() as client:
        r = await wormhole_celo_to_sol_direct(client, HUB_USDT)
        br = r.get("wormhole")
        _log(
            f"  {'OK' if r['success'] else 'FAIL'} src={getattr(br, 'source_tx', None)} "
            f"dst={getattr(br, 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_wormhole_claim()
        return r["success"]


async def step_hub_cross_chain() -> bool:
    """Run both hub triangles with claim workers between legs."""
    ok_a = await step_hub_celo_eth_sol()
    await step_cctp_claim()
    await step_wormhole_claim(max_rounds=60)
    await audit()
    ok_b = await step_hub_sol_eth_celo()
    await step_cctp_claim()
    await step_wormhole_claim(max_rounds=60)
    await audit()
    return ok_a and ok_b


async def step_closed_loop_celo(size: float = TEST_VCHF) -> bool:
    """Celo USDT → arb → return to Celo USDT when round-trip is economic."""
    from src.treasury.manager import TreasuryManager

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    treasury = TreasuryManager(chains, token, cfg)
    ex = ArbExecutor(chains, token, cfg)
    _log(f"\n=== Closed loop from Celo @ {size} VCHF ===")
    async with build_client() as client:
        result = await treasury.best_closed_loop_from_origin(client, ex, "celo", size)
        if not result:
            _log("  No economic closed loop from Celo")
            return False
        _log(
            f"  primary={result.primary_direction} return={result.return_direction} "
            f"closed={result.closed} reason={result.reason}"
        )
        return result.closed


async def step_cctp_sol_to_eth() -> bool:
    amount = await _hub_usdc_amount()
    if amount < PROBE_USDC:
        _log(f"\n=== CCTP Sol→ETH — SKIP (Sol USDC < {PROBE_USDC}) ===")
        return False
    _log(f"\n=== CCTP Sol→ETH ${amount:.2f} ===")
    bridge = CircleCctpBridge()
    async with build_client() as client:
        r = await bridge.bridge_usdc_sol_to_eth(client, amount)
        _log(f"  {'OK' if r.success else 'FAIL'} src={r.source_tx} dst={r.dest_tx} err={r.error}")
        if not r.success:
            await step_cctp_claim()
        return r.success or r.dest_tx is not None


async def step_eth_usdc_to_celo() -> bool:
    """Complete SOL→ETH→CELO triangle: ETH USDC → swap USDT → Wormhole → Celo."""
    from scripts.rebalance_for_test import _balances

    b = await _balances()
    amount = min(HUB_USDC, b.get("eth_usdc", 0) * 0.95)
    if amount < 1.0:
        _log("\n=== ETH USDC → CELO — SKIP (ETH USDC < 1) ===")
        return False
    _log(f"\n=== ETH USDC → swap USDT → Wormhole CELO (${amount:.2f} USDC) ===")
    async with build_client() as client:
        r = await wormhole_eth_to_celo_via_usdc(client, amount)
        _log(
            f"  {'OK' if r['success'] else 'PARTIAL/FAIL'} stage={r.get('stage', 'wormhole')} "
            f"swap={r.get('swap_tx')} wh={getattr(r.get('wormhole'), 'dest_tx', None)} err={r.get('error')}"
        )
        if not r["success"]:
            await step_wormhole_claim(max_rounds=60)
        return r["success"] or bool(r.get("swap_tx"))


async def step_cctp_eth_to_sol() -> bool:
    from scripts.rebalance_for_test import _balances

    b = await _balances()
    amount = min(CCTP_USDC, b.get("eth_usdc", 0) * 0.95)
    if amount < PROBE_USDC:
        _log(f"\n=== CCTP ETH→Sol — SKIP (ETH USDC {b.get('eth_usdc', 0):.2f}) ===")
        return False
    _log(f"\n=== CCTP ETH→Sol ${amount:.2f} ===")
    bridge = CircleCctpBridge()
    async with build_client() as client:
        r = await bridge.bridge_usdc_eth_to_sol(client, amount)
        _log(f"  {'OK' if r.success else 'FAIL'} src={r.source_tx} dst={r.dest_tx} err={r.error}")
        if not r.success:
            await step_cctp_claim()
        return r.success or r.dest_tx is not None


async def run_full_matrix() -> int:
    """
    Full live validation: all VCHF arb directions + CCTP (Sol↔ETH) + Wormhole preflight (Celo→Sol/ETH).
    VNX↔ETH settlement is exercised via solana_to_vnx / vnx_to_solana (CCTP legs).
    """
    os.environ["CCTP_RECONCILE_USDC"] = "0"
    os.environ["ENABLE_VNX_ARB_ROUTES"] = "true"
    os.environ["ENABLE_VNX_CCTP_ROUTES"] = "true"

    _log("\n========== FULL CROSS-CHAIN MATRIX ==========")
    if TX_LOG_PATH.exists():
        TX_LOG_PATH.write_text("", encoding="utf-8")

    await step_cctp_claim()
    _log("\n=== Initial balance audit ===")
    await audit()

    _log("\n=== Rebalance ===")
    rebal_ok = await step_rebalance(execute=True)
    if not rebal_ok:
        _log("WARN: rebalance incomplete — continuing with best-effort route order")
    await audit()
    await step_wormhole_claim()

    from src.scanner.routes import ALL_DIRECTIONS
    from scripts.rebalance_for_test import _balances, route_order_for_balances

    cfg = load_bot_config()
    b = await _balances()
    vchf_order = list(await route_order_for_balances(b))
    for d in ALL_DIRECTIONS:
        if d not in vchf_order:
            vchf_order.append(d)
    _log(f"\nVCHF route order: {vchf_order}")

    results: dict[str, bool | str] = {}

    for direction in vchf_order:
        _log(f"\n--- Pre-route rebalance ({direction}) ---")
        await step_rebalance(execute=True)
        await step_cctp_claim()
        _log(f"\n--- VCHF route: {direction} @ {TEST_VCHF} VCHF ---")
        try:
            results[direction] = await _force_exec(direction, TEST_VCHF)
        except Exception as exc:
            _log(f"CRASH {direction}: {exc}")
            results[direction] = False
        await step_cctp_claim()
        await audit()

    hub_steps = (
        ("eth_to_vnx", step_eth_to_vnx),
        ("vnx_to_eth", step_vnx_to_eth),
        ("wormhole_celo_to_eth", step_wormhole_celo_to_eth),
        ("wormhole_eth_to_celo", step_wormhole_eth_to_celo),
        ("celo_usdt_to_vnx", step_celo_usdt_to_vnx),
        ("hub_celo_eth_sol", step_hub_celo_eth_sol),
        ("hub_sol_eth_celo", step_hub_sol_eth_celo),
        ("wormhole_celo_to_sol_direct", step_wormhole_celo_to_sol),
    )
    for name, fn in hub_steps:
        _log(f"\n--- Hub route: {name} ---")
        await step_cctp_claim()
        await step_wormhole_claim(max_rounds=40)
        try:
            results[name] = await fn()
        except Exception as exc:
            _log(f"CRASH {name}: {exc}")
            results[name] = False
        await audit()

    bridge_steps = (
        ("cctp_sol_to_eth", step_cctp_sol_to_eth),
        ("cctp_eth_to_sol", step_cctp_eth_to_sol),
        ("wormhole_celo_sol_eth", step_wormhole_usdt_check),
        ("celo_swaps", step_celo_swaps),
        ("sol_swaps", step_sol_swaps),
    )
    for name, fn in bridge_steps:
        _log(f"\n--- Bridge/DEX probe: {name} ---")
        await step_cctp_claim()
        try:
            results[name] = await fn()
        except Exception as exc:
            _log(f"CRASH {name}: {exc}")
            results[name] = False
        await audit()

    # Sol-initiate wormhole reverse still requires SPL SDK
    for label in (
        "wormhole_sol_to_celo_usdt",
        "wormhole_eth_to_sol_usdt",
    ):
        results[label] = "N/A (Sol initiate — use CCTP for Sol↔ETH USDC)"

    await step_cctp_claim()
    await step_wormhole_claim()
    _log("\n=== Final balance audit ===")
    await audit()

    _log("\n=== Full matrix summary ===")
    for k, v in results.items():
        if v == "N/A (Sol initiate — use CCTP for Sol↔ETH USDC)":
            _log(f"  N/A     {k}")
        else:
            _log(f"  {'PASS' if v else 'FAIL'}  {k}")

    if TX_LOG_PATH.exists():
        _log(f"\n=== TX log ({TX_LOG_PATH}) ===")
        for line in TX_LOG_PATH.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            url = row.get("url") or ""
            _log(f"  {row.get('intent')} | {row.get('chain')} | {row.get('tx_hash')} {url}")

    fails = sum(1 for v in results.values() if v is False)
    return fails


async def run_production() -> int:
    """Full production validation: rebalance, claim CCTP, run all routes in capital-efficient order."""
    os.environ["CCTP_RECONCILE_USDC"] = "0"
    _log("\n========== PRODUCTION ROUTE TEST ==========")
    if TX_LOG_PATH.exists():
        TX_LOG_PATH.write_text("", encoding="utf-8")

    await step_cctp_claim()
    _log("\n=== Rebalance for test ===")
    rebal_ok = await step_rebalance(execute=True)
    if not rebal_ok:
        _log("WARN: rebalance incomplete — some routes may fail")
    await audit()

    cfg = load_bot_config()
    from scripts.rebalance_for_test import _balances, route_order_for_balances

    b = await _balances()
    base_order = await route_order_for_balances(b)
    directions = [d for d in base_order if d in active_directions(cfg)]
    _log(f"\nRoute order: {directions}")

    results: dict[str, bool] = {}

    for direction in directions:
        _log(f"\n--- Pre-route rebalance ({direction}) ---")
        await step_rebalance(execute=True)
        await step_cctp_claim()
        _log(f"\n--- Route: {direction} ---")
        try:
            results[direction] = await _force_exec(direction, TEST_VCHF)
        except Exception as exc:
            _log(f"CRASH {direction}: {exc}")
            results[direction] = False
        await step_cctp_claim()
        await audit()

    for probe in ("celo-swaps", "sol-swaps", "wormhole-usdt"):
        _log(f"\n--- Probe: {probe} ---")
        try:
            results[probe] = await STEPS[probe]()
        except Exception as exc:
            _log(f"CRASH {probe}: {exc}")
            results[probe] = False

    await step_cctp_claim()

    _log("\n=== Production summary ===")
    for k, v in results.items():
        _log(f"  {'PASS' if v else 'FAIL/SKIP'}  {k}")

    if TX_LOG_PATH.exists():
        _log(f"\n=== TX log ({TX_LOG_PATH}) ===")
        for line in TX_LOG_PATH.read_text(encoding="utf-8").strip().splitlines():
            row = json.loads(line)
            url = row.get("url") or ""
            _log(f"  {row.get('intent')} | {row.get('chain')} | {row.get('tx_hash')} {url}")

    fails = sum(1 for v in results.values() if not v)
    return fails


async def step_production_readiness() -> bool:
    from src.treasury.readiness import format_report, funding_report

    rows, balances = await funding_report("production")
    _log(format_report(rows, balances))
    return all(r.ok for r in rows)


async def step_platform_probe() -> bool:
    """Platform buy/sell round-trip at VNX minimum (30 VCHF order; probe uses TEST_VCHF)."""
    cfg = load_bot_config()
    size = TEST_VCHF
    _log(f"\n=== Platform probe buy/sell @ {size} VCHF ===")
    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        if vnx.usdc_balance(bal) < size * 1.35:
            _log(f"SKIP: platform USDC {vnx.usdc_balance(bal):.2f} < {size * 1.35:.0f}")
            return False
        buy = await platform_buy_vchf(cfg, size, vnx=vnx)
        if not buy.success:
            _log(f"FAIL buy: {buy.error}")
            return False
        log_platform_order("probe_platform_buy", buy.ordid, qty=buy.quantity)
        sell = await platform_sell_vchf(cfg, buy.quantity, vnx=vnx)
        if not sell.success:
            _log(f"FAIL sell: {sell.error}")
            return False
        log_platform_order("probe_platform_sell", sell.ordid, sold=sell.sold, currency=sell.sold_currency)
        _log(f"OK buy ordid={buy.ordid} sell ordid={sell.ordid}")
    return True


async def step_simulate_all_routes() -> bool:
    from src.scanner.routes import ALL_DIRECTIONS, active_directions
    from src.treasury.loops import origin_for_direction

    cfg = load_bot_config()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    active = set(active_directions(cfg))
    ok = True
    _log(f"\n=== Simulate all VCHF routes @ {TEST_VCHF} VCHF (quotes only) ===")
    async with build_client() as client:
        for direction in ALL_DIRECTIONS:
            sim = await simulate_direction(client, chains, token, cfg, direction, TEST_VCHF)
            tag = "act" if direction in active else "off"
            if sim.error:
                _log(f"  FAIL [{tag}] {direction}: {sim.error}")
                ok = False
            else:
                _log(
                    f"  OK   [{tag}] {direction} net=${sim.net_profit_usd:+.2f} "
                    f"stable_out=${sim.stable_out_usd:.2f}"
                )
    return ok


async def step_verify_all() -> bool:
    """Max verification: claims, readiness, bridge sims, DEX probes, route sims."""
    _log("\n========== VERIFY ALL (production preflight) ==========")
    results: dict[str, bool] = {}

    results["cctp_claim"] = await step_cctp_claim()
    results["wormhole_claim"] = await step_wormhole_claim(max_rounds=20)
    await audit()

    from src.treasury.readiness import format_report, funding_report

    prod_rows, prod_bal = await funding_report("production")
    _log(format_report(prod_rows, prod_bal))
    test_rows, test_bal = await funding_report("route_test")
    _log("\n=== Route-test minimum (31 VCHF matrix) ===")
    _log(format_report(test_rows, test_bal))

    results["wormhole_preflight"] = await step_wormhole_preflight()
    results["route_simulations"] = await step_simulate_all_routes()

    from scripts.rebalance_for_test import _balances

    b = await _balances()
    celo_usdt = b.get("celo_usdt", 0)
    celo_vchf = b.get("celo_vchf", 0)
    celo_wrapped = prod_bal.get("celo_usdt_wrapped_eth", 0)
    if celo_wrapped >= 0.01:
        _log(f"\n=== Celo wrapped USDT {celo_wrapped:.2f} — consolidating to canonical ===")
        await step_consolidate_celo_usdt()
        b = await _balances()
        celo_usdt = b.get("celo_usdt", 0)
    if celo_usdt >= PROBE_USDC or celo_vchf >= 0.5:
        results["celo_swaps"] = await step_celo_swaps()
    else:
        _log(f"\nSKIP celo-swaps (USDT {celo_usdt:.2f}, VCHF {celo_vchf:.2f})")
        results["celo_swaps"] = False

    if b.get("sol_usdc", 0) >= PROBE_USDC:
        results["sol_swaps"] = await step_sol_swaps()
    else:
        _log(f"\nSKIP sol-swaps (USDC {b.get('sol_usdc', 0):.2f} < {PROBE_USDC})")
        results["sol_swaps"] = False

    if b.get("platform_usdc", 0) >= TEST_VCHF * 1.35:
        results["platform_probe"] = await step_platform_probe()
    else:
        _log(f"\nSKIP platform probe (USDC {b.get('platform_usdc', 0):.2f})")
        results["platform_probe"] = False

    if b.get("eth_usdc", 0) >= HUB_USDC:
        results["eth_to_vnx"] = await step_eth_to_vnx()
    else:
        _log(f"\nSKIP eth→vnx (ETH USDC {b.get('eth_usdc', 0):.2f} < VNX min {HUB_USDC:.0f})")
        results["eth_to_vnx"] = False

    if b.get("platform_usdc", 0) >= HUB_USDC:
        results["vnx_to_eth"] = await step_vnx_to_eth()
    else:
        _log(f"\nSKIP vnx→eth (platform USDC {b.get('platform_usdc', 0):.2f} < {HUB_USDC})")
        results["vnx_to_eth"] = False

    if b.get("sol_usdc", 0) >= PROBE_USDC:
        results["cctp_sol_eth"] = await step_cctp_sol_to_eth()
        await step_cctp_claim()
    else:
        results["cctp_sol_eth"] = False

    if b.get("eth_usdc", 0) >= PROBE_USDC:
        results["cctp_eth_sol"] = await step_cctp_eth_to_sol()
        await step_cctp_claim()
    else:
        results["cctp_eth_sol"] = False

    await step_cctp_claim()
    await step_wormhole_claim(max_rounds=10)
    await audit()

    _log("\n=== Verify-all summary ===")
    for k, v in results.items():
        _log(f"  {'PASS' if v else 'FAIL/SKIP'}  {k}")

    critical = ("cctp_claim", "wormhole_claim", "wormhole_preflight", "route_simulations")
    return all(results.get(k) for k in critical)


async def step_profit_scan() -> None:
    """Live round-trip profit matrix (simulation only, no execution)."""
    from src.scanner.routes import ALL_DIRECTIONS, active_directions
    from src.scanner.simulator import simulate_round_trip
    from src.treasury.loops import origin_for_direction

    cfg = load_bot_config()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    active = set(active_directions(cfg))

    _log("\n=== Profit scan (round-trip simulation) ===")
    _log(f"min_profit=${cfg.min_profit_usd} active={sorted(active)}")

    async with build_client() as client:
        for size in (TEST_VCHF, cfg.min_trade_vchf):
            _log(f"\n--- @ {size:.0f} VCHF ---")
            for direction in ALL_DIRECTIONS:
                origin = origin_for_direction(direction)
                rt = await simulate_round_trip(
                    client, chains, token, cfg, direction, size, origin=origin
                )
                ret_p = rt.return_sim.net_profit_usd if rt.return_sim else 0.0
                ret_dir = rt.return_direction or "-"
                go = "YES" if rt.profitable else "no"
                _log(
                    f"  {direction:<18} act={'Y' if direction in active else 'N'} "
                    f"pri=${rt.primary.net_profit_usd:+.2f} ret={ret_dir} ${ret_p:+.2f} "
                    f"round=${rt.round_trip_profit_usd:+.2f} {go}"
                )
    return True


STEPS = {
    "audit": lambda: audit(),
    "readiness": step_production_readiness,
    "verify-all": step_verify_all,
    "cctp-claim": step_cctp_claim,
    "platform-buy": step_platform_buy,
    "platform-sell": step_platform_sell,
    "celo-swaps": step_celo_swaps,
    "sol-swaps": step_sol_swaps,
    "wormhole-usdt": step_wormhole_usdt_check,
    "vnx-to-sol": lambda: _force_exec("vnx_to_solana", _ROUTE_SIZE),
    "sol-to-vnx": lambda: _force_exec("solana_to_vnx", _ROUTE_SIZE),
    "sol-to-celo": lambda: _force_exec("solana_to_celo", _ROUTE_SIZE),
    "celo-to-sol": lambda: _force_exec("celo_to_solana", _ROUTE_SIZE),
    "celo-to-vnx": lambda: _force_exec("celo_to_vnx", _ROUTE_SIZE),
    "vnx-to-celo": lambda: _force_exec("vnx_to_celo", _ROUTE_SIZE),
    "cctp-sol-eth": step_cctp_sol_to_eth,
    "cctp-eth-sol": step_cctp_eth_to_sol,
    "wormhole-claim": step_wormhole_claim,
    "consolidate-celo-usdt": step_consolidate_celo_usdt,
    "eth-to-vnx": step_eth_to_vnx,
    "vnx-to-eth": step_vnx_to_eth,
    "wormhole-celo-eth": step_wormhole_celo_to_eth,
    "wormhole-eth-celo": step_wormhole_eth_to_celo,
    "celo-usdt-to-vnx": step_celo_usdt_to_vnx,
    "hub-celo-eth-sol": step_hub_celo_eth_sol,
    "hub-sol-eth-celo": step_hub_sol_eth_celo,
    "hub-cross-chain": step_hub_cross_chain,
    "eth-usdc-to-celo": step_eth_usdc_to_celo,
    "eth-usdt-to-sol": step_eth_usdt_to_sol,
    "wormhole-celo-sol": step_wormhole_celo_to_sol,
    "closed-loop-celo": step_closed_loop_celo,
    "rebalance": lambda: step_rebalance(execute=True),
    "production": run_production,
    "scan": step_profit_scan,
    "full-matrix": run_full_matrix,
}


async def run_all() -> int:
    return await run_full_matrix()


async def main() -> None:
    global _ROUTE_SIZE
    p = argparse.ArgumentParser()
    p.add_argument("--step", default="production", choices=["all", *STEPS.keys()])
    p.add_argument(
        "--size",
        type=float,
        default=TEST_VCHF,
        help=f"VCHF size for route force-exec steps (default {TEST_VCHF})",
    )
    args = p.parse_args()
    _ROUTE_SIZE = args.size
    if args.step == "all":
        rc = await run_full_matrix()
        sys.exit(0 if rc == 0 else 1)
    if args.step == "production":
        rc = await run_production()
        sys.exit(0 if rc == 0 else 1)
    if args.step == "full-matrix":
        rc = await run_full_matrix()
        sys.exit(0 if rc == 0 else 1)
    if args.step == "audit":
        await audit()
        return
    ok = await STEPS[args.step]()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
