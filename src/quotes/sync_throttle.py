"""Synchronous per-provider spacing for JSON-RPC and other blocking HTTP."""

from __future__ import annotations

import os
import random
import time

_lock_provider: dict[str, float] = {}

_DEFAULT_MS: dict[str, float] = {
    "jupiter": 1200.0,
    "vnx": 1200.0,
    "cctp": 1000.0,
    "base_rpc": 400.0,
    "solana_rpc": 800.0,
    "eth_rpc": 250.0,
    "blockscout": 1000.0,
    "default": 600.0,
}


def _interval_sec(provider: str) -> float:
    if provider == "solana_rpc":
        raw = os.getenv("SOL_RPC_MIN_INTERVAL_MS") or os.getenv(
            "API_SYNC_SOLANA_RPC_MS", str(_DEFAULT_MS["solana_rpc"])
        )
        return max(0.0, float(raw) / 1000.0)
    key = f"API_SYNC_{provider.upper()}_MS"
    default = _DEFAULT_MS.get(provider, _DEFAULT_MS["default"])
    raw = os.getenv(key, os.getenv("API_SYNC_DEFAULT_MS", str(default)))
    return max(0.0, float(raw) / 1000.0)


def sync_throttle(provider: str = "default") -> float:
    """Block until minimum spacing since last call for *provider*. Returns seconds slept."""
    interval = _interval_sec(provider)
    if interval <= 0:
        return 0.0
    now = time.monotonic()
    last = _lock_provider.get(provider, 0.0)
    wait = interval - (now - last)
    if wait > 0:
        time.sleep(wait)
    _lock_provider[provider] = time.monotonic()
    return max(0.0, wait)


def retry_backoff_sec(attempt: int, *, base: float | None = None, cap: float | None = None) -> float:
    base = base if base is not None else float(os.getenv("API_RETRY_BACKOFF_BASE_SEC", "2.0"))
    cap = cap if cap is not None else float(os.getenv("API_RETRY_BACKOFF_CAP_SEC", "60.0"))
    return min(cap, base * (2**attempt)) + random.uniform(0, 0.25)


def reset_sync_throttle() -> None:
    _lock_provider.clear()
