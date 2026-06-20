from __future__ import annotations

import logging
import os

from src.config_loader import ChainConfig
from src.execution.kyber_swap import swap_via_kyber

logger = logging.getLogger(__name__)

USE_KYBER_SWAP = os.getenv("USE_KYBER_SWAP", "true").lower() in ("1", "true", "yes")
MIN_SWAP_STABLE_OUT_RAW = int(os.getenv("MIN_SWAP_STABLE_OUT_RAW", "10000"))


def validate_swap_min_out(min_raw: int, *, label: str = "swap") -> str | None:
    if min_raw <= 0:
        return f"{label}: amount_out_min is zero"
    if min_raw < MIN_SWAP_STABLE_OUT_RAW:
        return f"{label}: amount_out_min below dust threshold ({min_raw} < {MIN_SWAP_STABLE_OUT_RAW})"
    return None


def _default_pool_fee(chain: ChainConfig) -> int:
    for pool in (chain.pools or {}).values():
        if pool.get("fee") is not None:
            return int(pool["fee"])
    return 3000


def swap_tokens(
    executor,
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    *,
    slippage_bps: int = 50,
    fee: int | None = None,
) -> str | None:
    """
    EVM swap: KyberSwap aggregator first, Uniswap V3 exactInputSingle fallback.
    """
    err = validate_swap_min_out(amount_out_min, label="swap")
    if err:
        logger.error("Rejecting swap: %s", err)
        return None
    if amount_in <= 0:
        logger.error("Rejecting swap: zero amount_in")
        return None
    if USE_KYBER_SWAP and chain.kyber_slug:
        tx = swap_via_kyber(
            executor,
            token_in,
            token_out,
            amount_in,
            amount_out_min,
            slippage_bps=slippage_bps,
        )
        if tx:
            return tx
        logger.info("Kyber swap failed (%s), falling back to Uniswap", executor.last_error)

    pool_fee = fee if fee is not None else _default_pool_fee(chain)
    return executor.swap_exact_input(token_in, token_out, amount_in, amount_out_min, fee=pool_fee)
