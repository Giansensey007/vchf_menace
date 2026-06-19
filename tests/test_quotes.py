import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.quotes.jupiter import quote
from src.quotes.types import ProviderQuote


@pytest.mark.asyncio
async def test_jupiter_quote_mock():
    amount = 1_000_000_000
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"outAmount": "1350000", "routePlan": []}

    with patch("src.quotes.jupiter.get_with_retry", new=AsyncMock(return_value=mock_resp)):
        q = await quote(AsyncMock(), "mint_in", "mint_out", amount)
    assert q.ok
    assert q.amount_out == 1_350_000


@pytest.mark.asyncio
async def test_vnx_platform_quote_mock():
    from src.quotes import vnx

    amount = 50_000_000_000_000_000_000  # 50 VCHF
    mock_quotes = {"VCHF/USDC": {"b": [1.35, 50000], "a": [1.36, 50000]}}

    with patch("src.quotes.vnx._load_quotes", new=AsyncMock(return_value=mock_quotes)):
        q = await vnx.quote_sell_token_for_usdc(AsyncMock(), "VCHF", amount, 18, 6)
    assert q.ok


@pytest.mark.asyncio
async def test_vnx_rejects_zero_bid_liquidity():
    from src.quotes import vnx

    amount = 50_000_000_000_000_000_000
    mock_quotes = {"VCHF/USDC": {"b": [1.35, 0], "a": [1.36, 50000]}}

    with patch("src.quotes.vnx._load_quotes", new=AsyncMock(return_value=mock_quotes)):
        q = await vnx.quote_sell_token_for_usdc(AsyncMock(), "VCHF", amount, 18, 6)
    assert not q.ok
    assert "bid liquidity" in (q.error or "")


@pytest.mark.asyncio
async def test_vnx_load_quotes_surfaces_http_errors():
    from src.quotes import vnx

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "service unavailable"

    with patch("src.quotes.vnx.VNX_API_PUBLIC_KEY", "test-key"):
        with patch("src.quotes.vnx.get_with_retry", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(RuntimeError, match="503"):
                await vnx._load_quotes(AsyncMock())


@pytest.mark.asyncio
async def test_rate_limit_releases_semaphore_during_backoff():
    from src.quotes import rate_limit

    rate_limit._api_sem = asyncio.Semaphore(1)
    client = AsyncMock()
    resp429 = MagicMock()
    resp429.status_code = 429
    resp429.text = "rate limited"
    resp200 = MagicMock()
    resp200.status_code = 200
    resp200.text = "ok"
    client.request = AsyncMock(side_effect=[resp429, resp200])

    acquired_during_backoff = False

    original_sleep = asyncio.sleep

    async def spy_sleep(sec):
        nonlocal acquired_during_backoff
        if rate_limit._api_sem.locked():
            acquired_during_backoff = True
        await original_sleep(0)

    with patch.object(rate_limit.asyncio, "sleep", side_effect=spy_sleep):
        with patch.object(rate_limit, "QUOTE_DELAY_MS", 0):
            with patch.object(rate_limit, "BACKOFF_BASE_SEC", 0.001):
                with patch.object(rate_limit, "api_sync", new=AsyncMock()):
                    r = await rate_limit.get_with_retry(client, "https://api.jup.ag/test")
    assert not acquired_during_backoff
    assert r.status_code == 200
    assert client.request.await_count == 2


def test_onchain_mock():
    from src.quotes.onchain import quote_pool

    w3 = MagicMock()
    contract = MagicMock()
    w3.eth.contract.return_value = contract
    contract.functions.quoteExactInputSingle.return_value.call.return_value = (900_000, 0, 0, 0)

    token_a = "0x" + "a" * 40
    token_b = "0x" + "b" * 40
    q = quote_pool(w3, "0x82825d0554fA07f7FC52Ab63c961F330fdEFa8E8", token_a, token_b, 1_000_000, 100)
    assert q.ok
