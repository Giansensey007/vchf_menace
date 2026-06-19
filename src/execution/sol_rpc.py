"""Solana JSON-RPC call wrapper with throttle and retry."""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Callable
from typing import TypeVar

from src.quotes.sync_throttle import retry_backoff_sec, sync_throttle

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_ATTEMPTS = int(os.getenv("RPC_RETRY_MAX", "4"))
SOL_BALANCE_POLL_SEC = float(os.getenv("SOL_BALANCE_POLL_SEC", "5"))


def sol_rpc_backoff_sec(attempt: int) -> float:
    """Exponential backoff for Solana RPC 429 / rate-limit responses."""
    base = float(
        os.getenv(
            "SOL_RPC_429_BACKOFF_SEC",
            os.getenv("API_RETRY_BACKOFF_BASE_SEC", "8.0"),
        )
    )
    cap = float(os.getenv("API_RETRY_BACKOFF_CAP_SEC", "120.0"))
    return min(cap, base * (2**attempt)) + random.uniform(0, 0.5)


def is_sol_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        k in msg
        for k in (
            "429",
            "too many requests",
            "rate limit",
            "blockhash",
            "block height",
            "blockhashnotfound",
            "transactionexpired",
            "timeout",
            "connection",
            "temporarily unavailable",
            "503",
            "502",
        )
    )


def is_jupiter_slippage_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "6024" in msg or "0x1788" in msg or "slippage" in msg


def call_with_retry(fn: Callable[[], T], *, label: str = "sol-rpc", max_attempts: int | None = None) -> T:
    attempts = max_attempts or _MAX_ATTEMPTS
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            sync_throttle("solana_rpc")
            return fn()
        except Exception as exc:
            last = exc
            if attempt + 1 >= attempts or not is_sol_retryable(exc):
                raise
            wait = (
                sol_rpc_backoff_sec(attempt)
                if any(k in str(exc).lower() for k in ("429", "too many requests", "rate limit"))
                else retry_backoff_sec(attempt)
            )
            logger.warning(
                "%s failed (attempt %s/%s): %s — retry in %.1fs",
                label,
                attempt + 1,
                attempts,
                exc,
                wait,
            )
            import time

            time.sleep(wait)
    if last:
        raise last
    raise RuntimeError(f"{label} failed with no exception")
