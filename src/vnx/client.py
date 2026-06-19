from __future__ import annotations

import json
import logging
import os
import time
import asyncio
from typing import Any

import httpx

from src.quotes.rate_limit import get_with_retry, post_with_retry
from src.vnx.auth import auth_headers, canonical_vnx_body, ensure_public_key_env, sort_object_deep
from src.vnx.collision import is_vnx_collision_error

logger = logging.getLogger(__name__)

VNX_API_BASE = os.getenv("VNX_API_BASE", "https://api.vnx.li/api/v1").rstrip("/")


class VnxClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._last_nonce = 0

    async def __aenter__(self) -> VnxClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        ensure_public_key_env()
        return self

    async def __aexit__(self, *_args) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("VnxClient not entered")
        return self._client

    async def get_assets(self) -> dict[str, Any]:
        path = "/api/v1/client/assets"
        url = f"{VNX_API_BASE}/client/assets"
        pub = ensure_public_key_env()
        resp = await get_with_retry(self.client, url, headers={"x-app-public-key": pub})
        resp.raise_for_status()
        return resp.json()

    async def get_quotes(self) -> dict[str, Any]:
        url = f"{VNX_API_BASE}/client/quotes"
        pub = ensure_public_key_env()
        last: dict[str, Any] = {"quotes": []}
        for attempt in range(8):
            resp = await get_with_retry(self.client, url, headers={"x-app-public-key": pub})
            resp.raise_for_status()
            data = resp.json()
            quotes = data.get("quotes") or []
            if quotes and quotes[0].get("symbol"):
                return data
            last = data
            if attempt < 7:
                wait = 8.0 if attempt == 0 else 4.0
                logger.debug("VNX quotes metadata-only response, retry in %.0fs", wait)
                await asyncio.sleep(wait)
        return last

    async def get_trading_pairs(self, status: str | None = None) -> dict[str, Any]:
        url = f"{VNX_API_BASE}/client/tradingPairs"
        pub = ensure_public_key_env()
        params = {"status": status} if status else None
        resp = await get_with_retry(self.client, url, headers={"x-app-public-key": pub}, params=params)
        resp.raise_for_status()
        return resp.json()

    def _next_nonce(self) -> int:
        candidate = int(time.time() * 1000)
        self._last_nonce = max(candidate, self._last_nonce + 1)
        return self._last_nonce

    async def _private_post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/v1/private/{endpoint}"
        url = f"{VNX_API_BASE}/private/{endpoint}"
        sorted_payload = sort_object_deep(payload)
        body = canonical_vnx_body(payload)
        nonce = self._next_nonce()
        headers = auth_headers(path, sorted_payload, nonce=nonce)
        resp = await post_with_retry(self.client, url, content=body.encode("utf-8"), headers=headers)
        if resp.status_code >= 400:
            body_text = resp.text[:500]
            if is_vnx_collision_error(body_text):
                logger.warning(
                    "VNX %s contention HTTP %s: %s",
                    endpoint,
                    resp.status_code,
                    body_text[:200],
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                err = data.get("error") or {}
                return {
                    "result": "error",
                    "error": {
                        "code": err.get("code") or f"http_{resp.status_code}",
                        "message": err.get("message") or body_text[:300],
                    },
                }
            logger.error("VNX %s HTTP %s: %s", endpoint, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "error":
            err = data.get("error") or {}
            msg = str(err.get("message") or err.get("code") or "")
            if is_vnx_collision_error(msg):
                logger.warning("VNX %s contention: %s", endpoint, msg[:200])
        return data

    async def account_balance(self) -> dict[str, Any]:
        return await self._private_post("accountBalance", {})

    async def deposit_address(self, asset: str, blockchain: str) -> dict[str, Any]:
        return await self._private_post("depositAddress", {"asset": asset, "blockchain": blockchain})

    async def withdraw_addresses(self, blockchain: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if blockchain:
            payload["blockchain"] = blockchain
        return await self._private_post("withdrawAddresses", payload)

    async def withdraw(self, asset: str, quantity: float, destination: str, *, blockchain: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"asset": asset, "quantity": quantity, "destination": destination}
        if blockchain:
            payload["blockchain"] = blockchain
        return await self._private_post("withdraw", payload)

    async def add_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._private_post("addOrder", payload)

    async def query_orders(self, **filters: Any) -> dict[str, Any]:
        return await self._private_post("queryOrders", filters)

    async def _private_post_optional(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Private POST that returns None on 403/404 (endpoint not enabled for this API key)."""
        path = f"/api/v1/private/{endpoint}"
        url = f"{VNX_API_BASE}/private/{endpoint}"
        sorted_payload = sort_object_deep(payload)
        body = canonical_vnx_body(payload)
        nonce = self._next_nonce()
        headers = auth_headers(path, sorted_payload, nonce=nonce)
        resp = await post_with_retry(self.client, url, content=body.encode("utf-8"), headers=headers)
        if resp.status_code in (403, 404):
            logger.warning("VNX %s not available (HTTP %s)", endpoint, resp.status_code)
            return None
        if resp.status_code >= 400:
            logger.error("VNX %s HTTP %s: %s", endpoint, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        return resp.json()

    async def query_withdrawals(self, **filters: Any) -> dict[str, Any] | None:
        return await self._private_post_optional("queryWithdrawals", filters)

    async def query_transfers(self, **filters: Any) -> dict[str, Any] | None:
        resp = await self._private_post_optional("queryTransfers", filters)
        if resp is not None:
            return resp
        return await self._private_post_optional("transfers", filters)

    def _asset_balance(self, balance_resp: dict[str, Any], asset: str) -> float:
        for row in balance_resp.get("balances") or []:
            if row.get("asset") == asset:
                return float(row.get("available_balance") or 0)
        return 0.0

    def vchf_balance(self, balance_resp: dict[str, Any]) -> float:
        return self._asset_balance(balance_resp, "VCHF")

    def usdc_balance(self, balance_resp: dict[str, Any]) -> float:
        return self._asset_balance(balance_resp, "USDC")

    def chf_balance(self, balance_resp: dict[str, Any]) -> float:
        return self._asset_balance(balance_resp, "CHF")
