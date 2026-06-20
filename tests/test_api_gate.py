"""Tests for per-provider API throttle (api_sync)."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from src.quotes import api_gate


@pytest.fixture(autouse=True)
def _reset_gate():
    api_gate.reset_api_sync()
    yield
    api_gate.reset_api_sync()


@pytest.mark.asyncio
async def test_api_sync_enforces_min_interval():
    with patch.dict(os.environ, {"API_SYNC_JUPITER_MS": "100"}, clear=False):
        t0 = asyncio.get_event_loop().time()
        await api_gate.api_sync("jupiter")
        await api_gate.api_sync("jupiter")
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed >= 0.09


@pytest.mark.asyncio
async def test_api_sync_different_providers_independent():
    with patch.dict(os.environ, {"API_SYNC_JUPITER_MS": "200", "API_SYNC_VNX_MS": "200"}, clear=False):
        t0 = asyncio.get_event_loop().time()
        await asyncio.gather(api_gate.api_sync("jupiter"), api_gate.api_sync("vnx"))
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.15


def test_provider_from_url():
    assert api_gate.provider_from_url("https://api.jup.ag/swap/v1/quote") == "jupiter"
    assert api_gate.provider_from_url("https://api.vnx.li/api/v1/quotes") == "vnx"
    assert api_gate.provider_from_url("https://iris-api.circle.com/v1/fees") == "cctp"
    assert api_gate.provider_from_url("https://forno.celo.org") == "base_rpc"
    assert api_gate.provider_from_url("https://example.com") == "default"
