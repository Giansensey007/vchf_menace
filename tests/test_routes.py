from src.scanner.routes import ALL_DIRECTIONS, ALL_ROUTES, estimate_fees_usd
from src.config_loader import load_bot_config

EXPECTED_DIRECTIONS = {
    "celo_to_solana",
    "solana_to_celo",
    "celo_to_vnx",
    "vnx_to_celo",
    "base_to_solana",
    "solana_to_base",
    "base_to_vnx",
    "vnx_to_base",
    "solana_to_vnx",
    "vnx_to_solana",
}


def test_all_ten_directions():
    assert len(ALL_ROUTES) == 10
    assert len(ALL_DIRECTIONS) == 10
    assert set(ALL_DIRECTIONS) == EXPECTED_DIRECTIONS


def test_vnx_routes_need_bridge():
    for r in ALL_ROUTES:
        if "vnx" in (r.buy_chain, r.sell_chain):
            assert r.needs_bridge


def test_route_fees_vnx_platform():
    cfg = load_bot_config()
    assert estimate_fees_usd("base", "vnx", cfg) > cfg.vnx_platform_fee_usd
    assert estimate_fees_usd("celo", "vnx", cfg) > cfg.vnx_platform_fee_usd


def test_base_sol_bridge_fee():
    cfg = load_bot_config()
    fee = estimate_fees_usd("base", "solana", cfg)
    assert fee >= cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd


def test_celo_sol_bridge_fee():
    cfg = load_bot_config()
    fee = estimate_fees_usd("celo", "solana", cfg)
    assert fee >= cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd


def test_vnx_arb_enabled_by_default():
    from src.scanner.routes import active_directions

    assert set(active_directions(load_bot_config())) == EXPECTED_DIRECTIONS


def test_active_routes_respects_env(monkeypatch):
    from src.scanner.routes import active_directions

    monkeypatch.setenv("ENABLE_VNX_ARB_ROUTES", "false")
    active = set(active_directions(load_bot_config()))
    assert "base_to_vnx" not in active
    assert "vnx_to_base" not in active
    assert "celo_to_vnx" not in active
    assert "vnx_to_celo" not in active
    assert len(active) == 6
