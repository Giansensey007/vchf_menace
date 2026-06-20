from unittest.mock import AsyncMock, patch

import pytest

from src.bridge.hub_usdt import usdt_raw_for_base_buy
from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import load_bridge_config, load_chains


def test_load_bridge_config():
    cfg = load_bridge_config()
    assert "wormhole" in cfg
    assert cfg["wormhole"]["base_usdc"].startswith("0x")
    assert cfg["hub"]["accounting_stable"] == "USDC"


def test_usdt_raw_for_base():
    assert usdt_raw_for_base_buy(100.0) == 100_000_000


def test_wormhole_quote_base_sol():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("base", "solana", 1000.0)
    assert q.ok
    assert q.amount_out_usdt < q.amount_in_usdt
    assert q.fee_usd > 0


def test_wormhole_quote_base_eth():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("base", "ethereum", 50.0)
    assert q.ok
    assert q.amount_out_usdt == pytest.approx(49.5)


def test_wormhole_quote_rejects_sol_to_eth():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("solana", "ethereum", 10.0)
    assert not q.ok


def test_wormhole_quote_rejects_same_chain():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("base", "base", 100.0)
    assert not q.ok


@pytest.mark.asyncio
async def test_wormhole_dry_run_base_to_eth(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    br = await wh.bridge_usdt_base_to_ethereum(10.0, "0x13D813Ca52577c55620091DFd3272cf2cdEae8F0")
    assert br.success
    assert br.dry_run


@pytest.mark.asyncio
async def test_wormhole_dry_run_base_to_sol(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    br = await wh.bridge_usdt_base_to_solana(10.0, "Gwacy3nVZdRf8FrmXf9JcTtK7ezGbu5zo6bFYuxSuMad")
    assert br.success
    assert br.dry_run


@pytest.mark.asyncio
async def test_usdc_to_usdt_jupiter_mock():
    from src.bridge.hub_usdt import usdc_to_usdt_solana

    with patch("src.bridge.hub_usdt.jupiter.quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = type("Q", (), {"ok": True, "amount_out": 99_500_000, "error": None})()
        human, raw = await usdc_to_usdt_solana(None, 100_000_000)
    assert human == pytest.approx(99.5)
    assert raw == 99_500_000
