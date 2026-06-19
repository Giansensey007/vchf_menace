from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


def build_client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(retries=2)
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=DEFAULT_LIMITS, transport=transport)
