from __future__ import annotations

import os

import httpx

from src.config_loader import ChainConfig
from src.quotes.addresses import checksum
from src.quotes.rate_limit import get_with_retry
from src.quotes.types import ProviderQuote

KYBER_BASE = os.getenv("KYBER_API_URL", "https://aggregator-api.kyberswap.com").rstrip("/")
KYBER_CLIENT_ID = os.getenv("KYBER_CLIENT_ID", "vchf-menace")


async def quote(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> ProviderQuote:
    if not chain.kyber_slug:
        return ProviderQuote("kyber", amount_in, 0, error="unsupported chain")
    token_in = checksum(token_in)
    token_out = checksum(token_out)
    url = f"{KYBER_BASE}/{chain.kyber_slug}/api/v1/routes"
    params = {
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amountIn": str(amount_in),
    }
    headers = {"X-Client-Id": KYBER_CLIENT_ID}
    try:
        resp = await get_with_retry(client, url, params=params, headers=headers, timeout=20.0)
        if resp.status_code == 429:
            return ProviderQuote("kyber", amount_in, 0, error="rate limited")
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                msg = err_body.get("message") or resp.text[:200]
            except Exception:
                msg = resp.text[:200]
            return ProviderQuote("kyber", amount_in, 0, error=str(msg))
        data = resp.json().get("data", {})
        summary = data.get("routeSummary") or {}
        amount_out = int(summary.get("amountOut") or 0)
        route_dexs = _extract_dexs(summary.get("route") or [])
        return ProviderQuote("kyber", amount_in, amount_out, route_dexs=route_dexs)
    except Exception as exc:
        return ProviderQuote("kyber", amount_in, 0, error=str(exc))


def _extract_dexs(route: list) -> list[str]:
    dexs: list[str] = []
    for hop in route:
        if isinstance(hop, list):
            for sub in hop:
                name = sub.get("exchange") or sub.get("pool") or "unknown"
                if name not in dexs:
                    dexs.append(str(name))
        elif isinstance(hop, dict):
            name = hop.get("exchange") or hop.get("pool") or "unknown"
            if name not in dexs:
                dexs.append(str(name))
    return dexs or ["KyberRoute"]
