from __future__ import annotations

import asyncio
import logging
import os

import httpx

from src.quotes.api_gate import api_sync, provider_from_url

logger = logging.getLogger(__name__)

MAX_RETRIES = int(os.getenv("API_RETRY_MAX", "6"))
BACKOFF_BASE_SEC = float(os.getenv("API_RETRY_BACKOFF_BASE_SEC", "3.0"))
BACKOFF_CAP_SEC = float(os.getenv("API_RETRY_BACKOFF_CAP_SEC", "120.0"))
CCTP_IRIS_429_BACKOFF_SEC = float(os.getenv("CCTP_IRIS_429_BACKOFF_SEC", "300"))
API_CONCURRENCY = int(os.getenv("QUOTE_CONCURRENCY", "1"))
QUOTE_DELAY_MS = float(os.getenv("QUOTE_DELAY_MS", "800"))

_api_sem: asyncio.Semaphore | None = None


def _get_api_sem() -> asyncio.Semaphore:
    global _api_sem
    if _api_sem is None:
        _api_sem = asyncio.Semaphore(API_CONCURRENCY)
    return _api_sem


def _backoff_sec(prov: str, attempt: int, status_code: int) -> float:
    if prov == "cctp" and status_code == 429:
        return max(CCTP_IRIS_429_BACKOFF_SEC, BACKOFF_BASE_SEC * (2**attempt))
    return min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2**attempt))


def _is_retryable(resp: httpx.Response) -> bool:
    if resp.status_code in (429, 502, 503, 504):
        return True
    if resp.status_code == 400 and "invalid_request_limit" in resp.text:
        return True
    if resp.status_code == 403 and "rate" in resp.text.lower():
        return True
    return False


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    prov = provider_from_url(url)
    last: httpx.Response | None = None
    for attempt in range(MAX_RETRIES):
        try:
            async with _get_api_sem():
                await api_sync(prov)
                resp = await client.request(method, url, **kwargs)
                last = resp
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            if attempt + 1 >= MAX_RETRIES:
                raise
            wait = _backoff_sec(prov, attempt, 503)
            logger.warning("HTTP %s %s network error: %s — retry in %.1fs", method, url[-40:], exc, wait)
            await asyncio.sleep(wait)
            continue
        if not _is_retryable(resp):
            if QUOTE_DELAY_MS > 0:
                await asyncio.sleep(QUOTE_DELAY_MS / 1000.0)
            return resp
        wait = _backoff_sec(prov, attempt, resp.status_code)
        logger.warning(
            "%s from %s — backing off %.1fs (attempt %s/%s)",
            resp.status_code,
            url.split("?")[0][-40:],
            wait,
            attempt + 1,
            MAX_RETRIES,
        )
        await asyncio.sleep(wait)
    if last is None:
        raise RuntimeError(f"no response from {url.split('?')[0][-40:]}")
    return last


async def get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await _request_with_retry(client, "GET", url, **kwargs)


async def post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await _request_with_retry(client, "POST", url, **kwargs)
