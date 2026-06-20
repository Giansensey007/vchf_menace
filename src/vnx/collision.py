from __future__ import annotations

import os
from typing import Any

# Substrings indicating VNX platform contention (GBP + VCHF + VNXAU share one account).
_COLLISION_MARKERS: tuple[str, ...] = (
    "invalid_nonce",
    "invalid nonce",
    "invalid_request_limit",
    "concurrent",
    "in flight",
    "inflight",
    "another order",
    "order in progress",
    "already processing",
    "busy",
    "limit exceeded",
    "order rejected",
    "rejected",
    "insufficient balance",
    "insufficient platform",
    "insufficient funds",
    "not enough balance",
    "withdraw rejected",
    "withdrawal rejected",
)


def collision_retry_max() -> int:
    return int(os.getenv("VNX_COLLISION_RETRY_MAX", "3"))


def collision_backoff_sec(attempt: int) -> float:
    base = float(os.getenv("VNX_COLLISION_BACKOFF_SEC", "5"))
    return base * (attempt + 1)


def is_vnx_collision_error(message: str | None) -> bool:
    """True when failure is likely from shared VNX account contention (non-fatal)."""
    if not message:
        return False
    text = message.lower()
    return any(marker in text for marker in _COLLISION_MARKERS)


def vnx_error_message(resp: dict[str, Any]) -> str | None:
    """Extract error text from a VNX private API response, if any."""
    if resp.get("result") != "error":
        return None
    err = resp.get("error") or {}
    msg = err.get("message") or err.get("code")
    return str(msg) if msg else "unknown VNX error"
