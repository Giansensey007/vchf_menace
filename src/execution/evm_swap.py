from __future__ import annotations

import logging
import os

from src.config_loader import ChainConfig
from src.execution.kyber_swap import swap_via_kyber

logger = logging.getLogger(__name__)

USE_KYBER_SWAP = os.getenv("USE_KYBER_SWAP", "true").lower() in ("1", "true", "yes")


def swap_tokens(
    executor,
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    *,
    slippage_bps: int = 50,
    fee: int = 3000,
) -> str | None:
    """
    EVM swap: KyberSwap aggregator first, Uniswap V3 exactInputSingle fallback.
    """
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

    return executor.swap_exact_input(token_in, token_out, amount_in, amount_out_min, fee=fee)
