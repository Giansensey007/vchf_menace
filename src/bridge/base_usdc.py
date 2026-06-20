"""Base USDC hub — canonical USDC on Base (Wormhole Base↔Sol/ETH)."""
from __future__ import annotations

import logging

from src.config_loader import load_bridge_config, load_chains
from src.execution.base import BaseExecutor
from src.execution.tx_log import log_tx
from src.quotes.types import from_human, to_human

logger = logging.getLogger(__name__)


def base_usdc_addresses() -> tuple[str, str]:
    """Return (canonical_usdc, optional_wrapped_usdc) from config."""
    wh = load_bridge_config()["wormhole"]
    chains = load_chains()
    canonical = chains["base"].hub_token
    wrapped = wh.get("base_usdc_wormhole_from_eth", canonical)
    return canonical, wrapped


def base_usdc_balances(base: BaseExecutor | None = None) -> dict[str, float]:
    """Balances of canonical and optional wrapped USDC on Base hot wallet."""
    exec_ = base or BaseExecutor(load_chains()["base"])
    canonical, wrapped = base_usdc_addresses()
    out = {
        "canonical": float(to_human(exec_.balance_erc20(canonical), 6)),
        "wrapped_eth": 0.0,
    }
    if wrapped.lower() != canonical.lower():
        out["wrapped_eth"] = float(to_human(exec_.balance_erc20(wrapped), 6))
    return out


def consolidate_wrapped_to_canonical(
    amount_usdc: float | None = None,
    *,
    slippage_bps: int = 50,
    base: BaseExecutor | None = None,
) -> dict:
    """No-op when Base hub is native USDC only (no wrapped token configured)."""
    canonical, wrapped = base_usdc_addresses()
    if wrapped.lower() == canonical.lower():
        return {"success": True, "skipped": True, "reason": "native USDC only", "amount_usdc": 0.0}

    exec_ = base or BaseExecutor(load_chains()["base"])
    wrapped_bal = exec_.balance_erc20(wrapped)
    if wrapped_bal <= 0:
        return {"success": True, "skipped": True, "reason": "no wrapped USDC", "amount_usdc": 0.0}

    amount_raw = wrapped_bal if amount_usdc is None else min(from_human(amount_usdc, 6), wrapped_bal)
    if amount_raw <= 0:
        return {"success": False, "error": "zero amount", "amount_usdc": 0.0}

    amount_human = float(to_human(amount_raw, 6))
    for fee in (100, 500, 3000):
        sim = exec_.simulate_swap(wrapped, canonical, amount_raw, fee)
        if sim:
            min_out = int(sim["amount_out"] * (1 - slippage_bps / 10_000))
            tx = exec_.swap_exact_input(wrapped, canonical, amount_raw, min_out, fee=fee)
            if tx:
                log_tx("base_consolidate_wrapped_usdc", "base", tx, extra={"amount_usdc": amount_human})
            return {
                "success": bool(tx),
                "tx": tx,
                "amount_usdc": amount_human,
                "expected_canonical": float(to_human(sim["amount_out"], 6)),
                "error": None if tx else "swap failed",
            }
    return {"success": False, "error": "no wrapped→canonical pool", "amount_usdc": amount_human}


async def consolidate_after_eth_to_base_redeem(base: BaseExecutor | None = None) -> dict:
    bals = base_usdc_balances(base)
    if bals["wrapped_eth"] < 0.01:
        return {"success": True, "skipped": True, "reason": "no wrapped balance"}
    return consolidate_wrapped_to_canonical(base=base)
