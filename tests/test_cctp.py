from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.bridge.cctp import CircleCctpBridge


@pytest.mark.asyncio
async def test_cctp_quote_rejects_unsupported_pair():
    bridge = CircleCctpBridge()
    async with httpx.AsyncClient() as client:
        # Celo is not a CCTP domain; same-chain is also invalid
        q = await bridge.quote_usdc(client, "celo", "solana", 100.0)
        same = await bridge.quote_usdc(client, "base", "base", 100.0)
    assert not q.ok
    assert q.error == "unsupported pair"
    assert not same.ok
    assert same.error == "unsupported pair"


@pytest.mark.asyncio
async def test_cctp_quote_supports_base_pairs():
    bridge = CircleCctpBridge()
    async with httpx.AsyncClient() as client:
        for src, dst in (
            ("base", "solana"),
            ("solana", "base"),
            ("base", "ethereum"),
            ("ethereum", "base"),
        ):
            q = await bridge.quote_usdc(client, src, dst, 100.0)
            assert q.ok, f"{src}->{dst} should be a supported direct CCTP route"
            assert q.amount_out_usdc == pytest.approx(q.amount_in_usdc - q.fee_usd)


@pytest.mark.asyncio
async def test_cctp_base_bridges_dry_run():
    bridge = CircleCctpBridge()
    with patch("src.bridge.cctp.is_dry_run", return_value=True):
        async with httpx.AsyncClient() as client:
            r1 = await bridge.bridge_usdc_base_to_eth(client, 100.0)
            r2 = await bridge.bridge_usdc_eth_to_base(client, 100.0)
            r3 = await bridge.bridge_usdc_base_to_sol(client, 100.0)
            r4 = await bridge.bridge_usdc_sol_to_base(client, 100.0)
    for r in (r1, r2, r3, r4):
        assert r.success and r.dry_run
        assert r.source_tx and r.dest_tx


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
