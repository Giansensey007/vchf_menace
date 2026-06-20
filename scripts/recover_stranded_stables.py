#!/usr/bin/env python3
"""Return stranded Sol USDC + Celo USDT to VNX platform via stable-only paths."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from scripts.execute_route_matrix import audit, step_cctp_claim, step_wormhole_claim
from src.bridge.cctp import CircleCctpBridge
from src.bridge.hub_eth import eth_usdc_to_vnx, swap_eth_usdt_to_usdc, vnx_usdc_to_eth
from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import load_bot_config, load_chains, load_tokens, token_decimals
from src.execution.celo import CeloExecutor
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.execution.tx_log import log_tx
from src.quotes.http_client import build_client
from src.quotes.types import from_human, to_human
from src.vnx.deposits import min_deposit_usdc, validate_eth_usdc_vnx_deposit


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _sol_usdc() -> float:
    chains = load_chains()
    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token))
    return float(sol.client.get_token_account_balance(ata).value.ui_amount or 0)


async def _celo_usdt() -> float:
    chains = load_chains()
    celo = CeloExecutor(chains["celo"])
    return float(to_human(celo.balance_erc20(chains["celo"].hub_token), chains["celo"].hub_decimals))


async def _eth_stables() -> tuple[float, float]:
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    usdc = float(to_human(eth.balance_erc20(chains["ethereum"].hub_token), 6))
    from src.config_loader import load_bridge_config

    usdt = float(to_human(eth.balance_erc20(load_bridge_config()["wormhole"]["ethereum_usdt"]), 6))
    return usdc, usdt


async def _sell_sol_vchf_if_any(client) -> str | None:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()
    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    vchf_ata = get_associated_token_address(sol.keypair.pubkey(), Pubkey.from_string(token.chains["solana"]))
    try:
        vchf = float(sol.client.get_token_account_balance(vchf_ata).value.ui_amount or 0)
    except Exception:
        vchf = 0.0
    if vchf < 0.01:
        return None
    dec = token_decimals(token, "solana")
    _log(f"--- Sell {vchf:.4f} Sol VCHF → USDC (stable recovery) ---")
    tx = await sol.swap(
        client, token.chains["solana"], chains["solana"].hub_token, from_human(vchf * 0.99, dec), cfg.slippage_bps
    )
    if tx:
        log_tx("recover_sol_sell_vchf", "solana", tx)
        _log(f"  OK sell tx={tx}")
    return tx


async def _cctp_sol_usdc_to_eth(client, amount: float) -> list[str]:
    if amount < 0.4:
        return []
    _log(f"--- CCTP Sol→ETH ${amount:.2f} USDC ---")
    bridge = CircleCctpBridge()
    br = await bridge.bridge_usdc_sol_to_eth(client, amount)
    txs = [t for t in (br.source_tx, br.dest_tx) if t]
    _log(f"  success={br.success} src={br.source_tx} dst={br.dest_tx} err={br.error}")
    if br.source_tx and not br.dest_tx:
        await step_cctp_claim()
    return txs


async def _wormhole_celo_usdt_to_eth(client, amount: float) -> list[str]:
    if amount < 0.5:
        return []
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    wh = WormholePortalBridge(chains["celo"])
    _log(f"--- Wormhole Celo→ETH ${amount:.2f} USDT ---")
    br = await wh.bridge_usdt_with_redeem(
        client,
        from_chain="celo",
        to_chain="ethereum",
        amount_usdt=amount,
        recipient=eth.address,
        intent="recover_celo_usdt_to_eth",
    )
    txs = [t for t in (br.source_tx, br.dest_tx) if t]
    _log(f"  success={br.success} src={br.source_tx} dst={br.dest_tx} err={br.error}")
    return txs


async def _swap_eth_usdt_to_usdc() -> str | None:
    _, usdt = await _eth_stables()
    amount = usdt * 0.95
    if amount < 0.1:
        return None
    _log(f"--- Uniswap ETH USDT→USDC ${amount:.2f} ---")
    swap = await swap_eth_usdt_to_usdc(amount)
    _log(f"  success={swap['success']} tx={swap.get('tx')} err={swap.get('error')}")
    return swap.get("tx")


async def _deposit_eth_usdc_to_vnx(client) -> str | None:
    usdc, _ = await _eth_stables()
    dep_err = validate_eth_usdc_vnx_deposit(usdc)
    if dep_err:
        _log(f"--- ETH→VNX deposit SKIP: {dep_err} ---")
        return None
    _log(f"--- ETH USDC→VNX deposit ${usdc:.2f} ---")
    r = await eth_usdc_to_vnx(client, usdc)
    _log(f"  success={r['success']} tx={r.get('deposit_tx')} err={r.get('error')}")
    if r.get("deposit_tx"):
        log_tx("recover_eth_usdc_to_vnx", "ethereum", r["deposit_tx"])
    return r.get("deposit_tx")


async def main() -> int:
    cfg = load_bot_config()
    eth_min = min_deposit_usdc("ETH")
    all_txs: list[str] = []

    _log("========== RECOVER STRANDED STABLES ==========")
    await step_wormhole_claim(max_rounds=40)
    await step_cctp_claim()
    await audit()

    async with build_client() as client:
        sol_amt = (await _sol_usdc()) * 0.95
        celo_amt = (await _celo_usdt()) * 0.95

        if sol_amt >= 0.4:
            all_txs.extend(await _cctp_sol_usdc_to_eth(client, sol_amt))
            await step_cctp_claim()

        if celo_amt >= 0.5:
            all_txs.extend(await _wormhole_celo_usdt_to_eth(client, celo_amt))
            await step_wormhole_claim(max_rounds=60)
            tx = await _swap_eth_usdt_to_usdc()
            if tx:
                all_txs.append(tx)

        # Pending VNX withdraw 5.4 VCHF→SOL may land during bridges
        for _ in range(12):
            sell_tx = await _sell_sol_vchf_if_any(client)
            if sell_tx:
                all_txs.append(sell_tx)
                extra = (await _sol_usdc()) * 0.95
                if extra >= 0.4:
                    all_txs.extend(await _cctp_sol_usdc_to_eth(client, extra))
                    await step_cctp_claim()
            await asyncio.sleep(15)

        usdc, usdt = await _eth_stables()
        _log(f"ETH stables after bridges: USDC={usdc:.2f} USDT={usdt:.2f} (min deposit {eth_min:.0f})")

        if usdc < eth_min and usdt >= 0.1:
            tx = await _swap_eth_usdt_to_usdc()
            if tx:
                all_txs.append(tx)
            usdc, usdt = await _eth_stables()

        if usdc < eth_min:
            need = eth_min - usdc + 1.0
            from src.vnx.client import VnxClient

            async with VnxClient() as vnx:
                bal = await vnx.account_balance()
                plat_usdc = vnx.usdc_balance(bal)
            withdraw = min(plat_usdc * 0.95, need)
            if withdraw >= 1.0:
                _log(f"--- VNX USDC→ETH withdraw ${withdraw:.2f} (top-up for min deposit) ---")
                wd = await vnx_usdc_to_eth(client, withdraw, cfg)
                _log(f"  success={wd['success']} txids={wd.get('withdraw_txids')} err={wd.get('error')}")
                if wd.get("withdraw_txids"):
                    all_txs.extend(wd["withdraw_txids"])
                deadline = time.time() + cfg.vnx_bridge_timeout_sec
                chains = load_chains()
                eth = EthereumExecutor(chains["ethereum"])
                target = from_human(withdraw * 0.9, 6)
                while time.time() < deadline:
                    await asyncio.sleep(12)
                    bal_raw = eth.balance_erc20(chains["ethereum"].hub_token)
                    if bal_raw >= target:
                        break
                usdc, _ = await _eth_stables()

        dep_tx = await _deposit_eth_usdc_to_vnx(client)
        if dep_tx:
            all_txs.append(dep_tx)

    await step_cctp_claim()
    await step_wormhole_claim(max_rounds=20)
    await audit()

    _log("\n=== RECOVERY TX HASHES ===")
    for tx in all_txs:
        _log(f"  {tx}")

    usdc, _ = await _eth_stables()
    sol_left = await _sol_usdc()
    celo_left = await _celo_usdt()
    _log(f"\nResidual: Sol USDC={sol_left:.2f} Celo USDT={celo_left:.2f} ETH USDC={usdc:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
