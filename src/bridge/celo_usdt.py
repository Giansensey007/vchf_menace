"""Celo USDT — canonical hub vs Wormhole-wrapped ETH-origin USDT."""
from __future__ import annotations

import logging

from src.config_loader import load_bridge_config, load_chains
from src.execution.celo import CeloExecutor
from src.execution.tx_log import log_tx
from src.quotes.types import from_human, to_human

logger = logging.getLogger(__name__)

# Uniswap V3 USDT/USDT (canonical ↔ wrapped) on Celo — fee tier 100
CELO_USDT_PAIR_FEE = 100


def celo_usdt_addresses() -> tuple[str, str]:
    """Return (canonical_usdt, wrapped_eth_usdt) from config."""
    wh = load_bridge_config()["wormhole"]
    chains = load_chains()
    canonical = chains["celo"].hub_token
    wrapped = wh["celo_usdt_wormhole_from_eth"]
    return canonical, wrapped


def celo_usdt_balances(celo: CeloExecutor | None = None) -> dict[str, float]:
    """Balances of canonical and wrapped USDT on Celo hot wallet."""
    chains = load_chains()
    exec_ = celo or CeloExecutor(chains["celo"])
    canonical, wrapped = celo_usdt_addresses()
    return {
        "canonical": float(to_human(exec_.balance_erc20(canonical), 6)),
        "wrapped_eth": float(to_human(exec_.balance_erc20(wrapped), 6)),
    }


def consolidate_wrapped_to_canonical(
    amount_usdt: float | None = None,
    *,
    slippage_bps: int = 50,
    celo: CeloExecutor | None = None,
) -> dict:
    """
    Swap Wormhole-wrapped ETH-USDT → canonical Celo USDT (same pool the bot uses for VCHF).

    Wrapped USDT is not used for Celo→ETH/Sol Wormhole outbound — consolidate after ETH→Celo redeems.
    """
    chains = load_chains()
    exec_ = celo or CeloExecutor(chains["celo"])
    canonical, wrapped = celo_usdt_addresses()
    wrapped_bal = exec_.balance_erc20(wrapped)
    if wrapped_bal <= 0:
        return {"success": True, "skipped": True, "reason": "no wrapped USDT", "amount_usdt": 0.0}

    if amount_usdt is None:
        amount_raw = wrapped_bal
    else:
        amount_raw = min(from_human(amount_usdt, 6), wrapped_bal)
    if amount_raw <= 0:
        return {"success": False, "error": "zero amount", "amount_usdt": 0.0}

    amount_human = float(to_human(amount_raw, 6))
    sim = exec_.simulate_swap(wrapped, canonical, amount_raw, CELO_USDT_PAIR_FEE)
    if not sim:
        return {
            "success": False,
            "error": "no canonical/wrapped USDT pool quote on Celo",
            "amount_usdt": amount_human,
        }
    min_out = int(sim["amount_out"] * (1 - slippage_bps / 10_000))
    tx = exec_.swap_exact_input(wrapped, canonical, amount_raw, min_out, fee=CELO_USDT_PAIR_FEE)
    if tx:
        log_tx("celo_consolidate_wrapped_usdt", "celo", tx, extra={"amount_usdt": amount_human})
        logger.info("Consolidated %.4f wrapped → canonical USDT on Celo: %s", amount_human, tx)
    return {
        "success": bool(tx),
        "tx": tx,
        "amount_usdt": amount_human,
        "expected_canonical": float(to_human(sim["amount_out"], 6)),
        "error": None if tx else "swap failed",
    }


async def consolidate_after_eth_to_celo_redeem(celo: CeloExecutor | None = None) -> dict:
    """Consolidate all wrapped USDT after an ETH→Celo Wormhole redeem lands on Celo."""
    bals = celo_usdt_balances(celo)
    if bals["wrapped_eth"] < 0.01:
        return {"success": True, "skipped": True, "reason": "no wrapped balance"}
    return consolidate_wrapped_to_canonical(celo=celo)
