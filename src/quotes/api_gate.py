from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Minimum spacing between outbound calls per provider (milliseconds)
# Jupiter: 2500 keyless (0.5 RPS), 1200 with free API key (1 RPS) — see dev.jup.ag/docs/portal/rate-limits
def _jupiter_default_ms() -> float:
    if os.getenv("JUPITER_API_KEY", "").strip():
        return 1200.0
    return 2500.0


_DEFAULT_SYNC_MS: dict[str, float] = {
    "jupiter": _jupiter_default_ms(),
    "vnx": 1200.0,
    "cctp": 1000.0,
    "base_rpc": 400.0,
    "solana_rpc": 250.0,
    "eth_rpc": 250.0,
    "blockscout": 1000.0,
    "default": 600.0,
}

_lock = asyncio.Lock()
_last_call_mono: dict[str, float] = {}


def _sync_ms(provider: str) -> float:
    key = f"API_SYNC_{provider.upper()}_MS"
    if provider == "jupiter" and key not in os.environ:
        default = _jupiter_default_ms()
    else:
        default = _DEFAULT_SYNC_MS.get(provider, _DEFAULT_SYNC_MS["default"])
    return float(os.getenv(key, os.getenv("API_SYNC_DEFAULT_MS", default)))


def provider_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "jup.ag" in host or "jupiter" in host:
        return "jupiter"
    if "vnx.li" in host:
        return "vnx"
    if "circle.com" in host or "iris-api" in host:
        return "cctp"
    if "base" in host or "forno" in host:
        return "base_rpc"
    if "solana" in host or host.endswith(".solana.com"):
        return "solana_rpc"
    if "llamarpc" in host or "publicnode.com" in host or "ankr.com" in host or "1rpc.io" in host:
        return "eth_rpc"
    if "blockscout" in host:
        return "blockscout"
    return "default"


async def api_sync(provider: str | None = None, *, url: str | None = None) -> float:
    """
    Wait until the per-provider minimum interval has elapsed since the last call.
    Returns seconds actually slept (0 if no wait needed).
    """
    prov = provider or (provider_from_url(url) if url else "default")
    interval = _sync_ms(prov) / 1000.0
    if interval <= 0:
        return 0.0

    async with _lock:
        now = time.monotonic()
        last = _last_call_mono.get(prov, 0.0)
        wait = interval - (now - last)
        if wait > 0:
            logger.debug("api_sync %s: sleeping %.2fs", prov, wait)
            await asyncio.sleep(wait)
            slept = wait
        else:
            slept = 0.0
        _last_call_mono[prov] = time.monotonic()
        return slept


def reset_api_sync() -> None:
    """Clear throttle state (for tests)."""
    _last_call_mono.clear()


async def stagger_delay_ms(ms: float | None = None) -> None:
    """Generic pause between high-level scan phases (sanity agents, route groups)."""
    delay = ms if ms is not None else float(os.getenv("API_STAGGER_MS", "1500"))
    if delay > 0:
        await asyncio.sleep(delay / 1000.0)
