from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import httpx

from src.config_loader import ChainConfig
from src.quotes.addresses import checksum

logger = logging.getLogger(__name__)

KYBER_BASE = os.getenv("KYBER_API_URL", "https://aggregator-api.kyberswap.com").rstrip("/")
KYBER_CLIENT_ID = os.getenv("KYBER_CLIENT_ID", "vchf-menace")


class _EvmSwapExecutor(Protocol):
    account: Any
    chain: ChainConfig
    last_error: str | None

    def approve_if_needed(self, token: str, spender: str, amount: int) -> str | None: ...
    def _build_and_send(self, tx: dict, *, fn=None) -> str | None: ...


def _kyber_headers() -> dict[str, str]:
    return {"X-Client-Id": KYBER_CLIENT_ID, "Content-Type": "application/json"}


def fetch_route(
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
) -> tuple[dict | None, int]:
    """Return (routeSummary, amountOut) from KyberSwap v1 routes API."""
    if not chain.kyber_slug or amount_in <= 0:
        return None, 0
    url = f"{KYBER_BASE}/{chain.kyber_slug}/api/v1/routes"
    params = {
        "tokenIn": checksum(token_in),
        "tokenOut": checksum(token_out),
        "amountIn": str(amount_in),
    }
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(url, params=params, headers=_kyber_headers())
            if resp.status_code >= 400:
                logger.warning("Kyber route HTTP %s: %s", resp.status_code, resp.text[:160])
                return None, 0
            data = resp.json().get("data", {})
            summary = data.get("routeSummary") or {}
            amount_out = int(summary.get("amountOut") or 0)
            if amount_out <= 0:
                return None, 0
            return summary, amount_out
    except Exception as exc:
        logger.warning("Kyber route fetch failed: %s", exc)
        return None, 0


def build_swap_tx(
    chain: ChainConfig,
    route_summary: dict,
    sender: str,
    *,
    slippage_bps: int = 50,
) -> dict | None:
    """Encode swap calldata via KyberSwap v1 route/build."""
    if not chain.kyber_slug:
        return None
    url = f"{KYBER_BASE}/{chain.kyber_slug}/api/v1/route/build"
    wallet = checksum(sender)
    payload = {
        "routeSummary": route_summary,
        "sender": wallet,
        "recipient": wallet,
        "slippageTolerance": max(1, slippage_bps),
    }
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(url, json=payload, headers=_kyber_headers())
            if resp.status_code >= 400:
                logger.warning("Kyber build HTTP %s: %s", resp.status_code, resp.text[:160])
                return None
            body = resp.json().get("data") or {}
            if not body.get("data") or not body.get("routerAddress"):
                return None
            return body
    except Exception as exc:
        logger.warning("Kyber build failed: %s", exc)
        return None


def swap_via_kyber(
    executor: _EvmSwapExecutor,
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    *,
    slippage_bps: int = 50,
) -> str | None:
    """Execute swap through KyberSwap aggregator router."""
    chain = executor.chain
    route_summary, quoted_out = fetch_route(chain, token_in, token_out, amount_in)
    if not route_summary:
        executor.last_error = "kyber: no route"
        return None
    if quoted_out < amount_out_min:
        executor.last_error = f"kyber quote {quoted_out} < min {amount_out_min}"
        return None

    built = build_swap_tx(chain, route_summary, executor.account.address, slippage_bps=slippage_bps)
    if not built:
        executor.last_error = "kyber: build route failed"
        return None

    router = checksum(built["routerAddress"])
    executor.approve_if_needed(token_in, router, amount_in)
    tx = {
        "from": executor.account.address,
        "to": router,
        "data": built["data"],
        "value": int(built.get("transactionValue") or 0),
    }
    try:
        tx["gas"] = executor.w3.eth.estimate_gas(tx)  # type: ignore[attr-defined]
    except Exception:
        tx["gas"] = int(built.get("gas") or 500_000)

    if hasattr(executor, "_tx_base"):
        base = executor._tx_base()  # type: ignore[attr-defined]
    else:
        base = executor._base_tx(type("_Fn", (), {"estimate_gas": lambda _s, _x: tx["gas"]})())  # type: ignore[attr-defined]

    for key in ("nonce", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "chainId"):
        if key in base:
            tx[key] = base[key]
    if "gas" not in tx and "gas" in base:
        tx["gas"] = base["gas"]
    return executor._build_and_send(tx)
