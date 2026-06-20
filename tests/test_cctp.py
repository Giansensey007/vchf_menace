from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.bridge.cctp import CircleCctpBridge


@pytest.mark.asyncio
async def test_cctp_quote_rejects_unsupported_pair():
    bridge = CircleCctpBridge()
    async with httpx.AsyncClient() as client:
        q = await bridge.quote_usdc(client, "base", "solana", 100.0)
    assert not q.ok
    assert q.error == "unsupported pair"


@pytest.mark.asyncio
async def test_cctp_quote_parses_iris_bps():
    bridge = CircleCctpBridge()
    mock_resp = httpx.Response(
        200,
        json=[
            {"finalityThreshold": 1000, "minimumFee": 1},
            {"finalityThreshold": 2000, "minimumFee": 0},
        ],
        request=httpx.Request("GET", "https://iris-api.circle.com/v2/burn/USDC/fees/5/0"),
    )
    with patch("src.bridge.cctp.get_with_retry", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        async with httpx.AsyncClient() as client:
            q = await bridge.quote_usdc(client, "solana", "ethereum", 1000.0)
    assert q.ok
    assert q.fee_usd == pytest.approx(0.10)  # 1 bps of 1000 USDC
    assert q.amount_out_usdc == pytest.approx(999.90)


@pytest.mark.asyncio
async def test_cctp_quote_live():
    """Live Iris fee lookup (skipped if network unavailable)."""
    bridge = CircleCctpBridge()
    async with httpx.AsyncClient() as client:
        q = await bridge.quote_usdc(client, "solana", "ethereum", 1000.0)
    assert q.ok
    assert q.fee_usd < 1.0  # live bps fee should beat static $1.50 estimate
    assert q.amount_out_usdc == pytest.approx(q.amount_in_usdc - q.fee_usd)
