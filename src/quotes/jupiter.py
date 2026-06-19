from __future__ import annotations

import os

import httpx

from src.quotes.rate_limit import get_with_retry
from src.quotes.types import ProviderQuote

JUPITER_QUOTE = "https://api.jup.ag/swap/v1/quote"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()


async def quote(
    client: httpx.AsyncClient,
    token_in: str,
    token_out: str,
    amount_in: int,
    slippage_bps: int | None = None,
) -> ProviderQuote:
    if amount_in <= 0:
        return ProviderQuote("jupiter", amount_in, 0, error="zero amount")
    bps = slippage_bps if slippage_bps is not None else int(os.getenv("SLIPPAGE_BPS", "50"))
    params = {
        "inputMint": token_in,
        "outputMint": token_out,
        "amount": str(amount_in),
        "slippageBps": bps,
        "restrictIntermediateTokens": "true",
    }
    headers: dict[str, str] = {}
    if JUPITER_API_KEY:
        headers["x-api-key"] = JUPITER_API_KEY
    try:
        resp = await get_with_retry(
            client, JUPITER_QUOTE, params=params, headers=headers or None, timeout=25.0
        )
        if resp.status_code == 429:
            return ProviderQuote("jupiter", amount_in, 0, error="rate limited")
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = body.get("error") or body.get("message") or resp.text[:200]
            except Exception:
                msg = resp.text[:200]
            return ProviderQuote("jupiter", amount_in, 0, error=str(msg)[:200])
        data = resp.json()
        if data.get("error"):
            return ProviderQuote("jupiter", amount_in, 0, error=str(data["error"])[:200])
        amount_out = int(data.get("outAmount") or 0)
        route_plan = data.get("routePlan") or []
        dexs = list(
            {
                step.get("swapInfo", {}).get("label", "Jupiter")
                for step in route_plan
                if step.get("swapInfo", {}).get("label")
            }
        )
        return ProviderQuote("jupiter", amount_in, amount_out, route_dexs=dexs or ["Jupiter"])
    except Exception as exc:
        return ProviderQuote("jupiter", amount_in, 0, error=str(exc))
