import pytest

from src.bridge.wormhole import WormholePortalBridge
from src.bridge.wormhole_vaa import token_bridge_emitter
from src.config_loader import load_bridge_config, load_chains


def test_token_bridge_emitter_padding():
    wh = load_bridge_config()["wormhole"]
    emitter = token_bridge_emitter(wh["base_token_bridge"])
    assert len(emitter) == 64
    assert emitter.endswith(wh["base_token_bridge"][2:].lower())


def test_wormhole_quote_eth_base():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("ethereum", "base", 25.0)
    assert q.ok
    assert q.amount_out_usdt == pytest.approx(24.5)


def test_wormhole_quote_base_eth_still_ok():
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    q = wh.quote_usdt("base", "ethereum", 25.0)
    assert q.ok


@pytest.mark.asyncio
async def test_wormhole_dry_run_eth_to_base(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    base = load_chains()["base"]
    wh = WormholePortalBridge(base)
    br = await wh.bridge_usdt_ethereum_to_base(10.0, "0x13D813Ca52577c55620091DFd3272cf2cdEae8F0")
    assert br.success
    assert br.dry_run
