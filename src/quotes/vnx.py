from __future__ import annotations

import logging
import os
import time

import httpx

from src.quotes.rate_limit import get_with_retry
from src.quotes.types import ProviderQuote, from_human, to_human

logger = logging.getLogger(__name__)

VNX_API_BASE = os.getenv("VNX_API_BASE", "https://api.vnx.io/api/v1").rstrip("/")
VNX_API_PUBLIC_KEY = os.getenv("VNX_API_PUBLIC_KEY", "").strip()
VNX_QUOTE_CACHE_SEC = float(os.getenv("VNX_QUOTE_CACHE_SEC", "1.0"))

VNX_MIN_ORDER: dict[str, float] = {"VCHF": 30.0}

VNX_QUOTE_CURRENCY: dict[str, str] = {
    "VGBP": "GBP",
    "VCHF": "CHF",
    "VNXAU": "USD",
}

_quote_cache: dict[str, dict] | None = None
_cache_at: float = 0.0


def _pair_symbols(token_symbol: str) -> list[str]:
    quote = VNX_QUOTE_CURRENCY.get(token_symbol, "USDC")
    primary = f"{token_symbol}/{quote}"
    legacy = f"{token_symbol}/USDC"
    if primary == legacy:
        return [primary]
    return [primary, legacy]


def _lookup_quote(quotes: dict[str, dict], token_symbol: str) -> tuple[str | None, dict | None]:
    for pair in _pair_symbols(token_symbol):
        q = quotes.get(pair)
        if q:
            return pair, q
    return None, None


async def _load_quotes(client: httpx.AsyncClient) -> dict[str, dict]:
    global _quote_cache, _cache_at
    now = time.time()
    if _quote_cache is not None and now - _cache_at < VNX_QUOTE_CACHE_SEC:
        return _quote_cache

    if not VNX_API_PUBLIC_KEY:
        raise ValueError("VNX_API_PUBLIC_KEY not set")

    url = f"{VNX_API_BASE}/client/quotes"
    headers = {"x-app-public-key": VNX_API_PUBLIC_KEY}
    resp = await get_with_retry(client, url, headers=headers, timeout=20.0)
    if resp.status_code == 401:
        raise RuntimeError("VNX quotes unauthorized (check VNX_API_PUBLIC_KEY)")
    if resp.status_code == 429 or (
        resp.status_code == 400 and "invalid_request_limit" in resp.text
    ):
        raise RuntimeError("VNX quotes rate limited after retries")
    if resp.status_code >= 400:
        logger.warning("VNX quotes HTTP %s: %s", resp.status_code, resp.text[:120])
        raise RuntimeError(f"VNX quotes HTTP {resp.status_code}")
    data = resp.json()
    quotes = data.get("quotes") or []
    if quotes and quotes[0].get("pair") and not quotes[0].get("symbol"):
        raise RuntimeError("VNX quotes returned pair metadata (no bid/ask) — retry later")
    by_symbol = {q["symbol"]: q for q in quotes if q.get("symbol")}
    _quote_cache = by_symbol
    _cache_at = now
    return by_symbol


def _price_and_liq(side: dict | list | None) -> tuple[float, float]:
    if not side or not isinstance(side, list) or len(side) < 2:
        return 0.0, 0.0
    try:
        return float(side[0]), float(side[1])
    except (TypeError, ValueError):
        return 0.0, 0.0


async def quote_sell_token_for_usdc(
    client: httpx.AsyncClient,
    token_symbol: str,
    amount_in: int,
    token_decimals: int,
    hub_decimals: int = 6,
) -> ProviderQuote:
    if amount_in <= 0:
        return ProviderQuote("vnx", amount_in, 0, error="zero amount")

    human_in = float(to_human(amount_in, token_decimals))
    min_sz = VNX_MIN_ORDER.get(token_symbol, 0.0)
    if min_sz and human_in < min_sz:
        return ProviderQuote("vnx", amount_in, 0, error=f"below VNX min order ({min_sz} {token_symbol})")

    try:
        quotes = await _load_quotes(client)
    except Exception as exc:
        return ProviderQuote("vnx", amount_in, 0, error=str(exc))

    pair, q = _lookup_quote(quotes, token_symbol)
    if not q:
        tried = ", ".join(_pair_symbols(token_symbol))
        return ProviderQuote("vnx", amount_in, 0, error=f"no VNX quote for {tried}")

    bid_price, bid_liq = _price_and_liq(q.get("b"))
    if bid_price <= 0:
        return ProviderQuote("vnx", amount_in, 0, error="no bid on VNX")
    if bid_liq <= 0:
        return ProviderQuote("vnx", amount_in, 0, error="no VNX bid liquidity")

    fill_tokens = min(human_in, bid_liq)
    if fill_tokens < human_in * 0.999:
        return ProviderQuote(
            "vnx", amount_in, 0, error=f"insufficient VNX bid liquidity ({bid_liq} {token_symbol})"
        )

    usdc_out = fill_tokens * bid_price
    amount_out = int(from_human(usdc_out, hub_decimals))
    if amount_out <= 0:
        return ProviderQuote("vnx", amount_in, 0, error="zero USDC output")

    return ProviderQuote(
        "vnx", amount_in, amount_out, route_dexs=[f"VNX bid {pair} @ {bid_price:.6f}"]
    )


async def quote_buy_token_with_usdc(
    client: httpx.AsyncClient,
    token_symbol: str,
    stable_amount: int,
    token_decimals: int,
    hub_decimals: int = 6,
) -> ProviderQuote:
    if stable_amount <= 0:
        return ProviderQuote("vnx", stable_amount, 0, error="zero amount")

    usdc_in = float(to_human(stable_amount, hub_decimals))
    if usdc_in <= 0:
        return ProviderQuote("vnx", stable_amount, 0, error="zero USDC input")

    try:
        quotes = await _load_quotes(client)
    except Exception as exc:
        return ProviderQuote("vnx", stable_amount, 0, error=str(exc))

    pair, q = _lookup_quote(quotes, token_symbol)
    if not q:
        tried = ", ".join(_pair_symbols(token_symbol))
        return ProviderQuote("vnx", stable_amount, 0, error=f"no VNX quote for {tried}")

    ask_price, ask_liq = _price_and_liq(q.get("a"))
    if ask_price <= 0:
        return ProviderQuote("vnx", stable_amount, 0, error="no ask on VNX")
    if ask_liq <= 0:
        return ProviderQuote("vnx", stable_amount, 0, error="no VNX ask liquidity")

    tokens_wanted = usdc_in / ask_price
    min_sz = VNX_MIN_ORDER.get(token_symbol, 0.0)
    if min_sz and tokens_wanted < min_sz:
        return ProviderQuote(
            "vnx", stable_amount, 0, error=f"below VNX min order ({min_sz} {token_symbol})"
        )

    if tokens_wanted > ask_liq * 0.999:
        return ProviderQuote(
            "vnx", stable_amount, 0, error=f"insufficient VNX ask liquidity ({ask_liq} {token_symbol})"
        )

    amount_out = int(from_human(tokens_wanted, token_decimals))
    if amount_out <= 0:
        return ProviderQuote("vnx", stable_amount, 0, error="zero token output")

    return ProviderQuote(
        "vnx", stable_amount, amount_out, route_dexs=[f"VNX ask {pair} @ {ask_price:.6f}"]
    )


def clear_quote_cache() -> None:
    global _quote_cache, _cache_at
    _quote_cache = None
    _cache_at = 0.0
