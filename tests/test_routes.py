from src.scanner.routes import ALL_DIRECTIONS, ALL_ROUTES, RouteSpec, estimate_fees_usd
from src.config_loader import load_bot_config


def test_all_eight_directions():
    assert len(ALL_ROUTES) == 8
    assert len(ALL_DIRECTIONS) == 8
    expected = {
        "base_to_solana",
        "solana_to_base",
        "base_to_vnx",
        "vnx_to_base",
        "solana_to_vnx",
        "vnx_to_solana",
        "ethereum_to_vnx",
        "vnx_to_ethereum",
    }
    assert set(ALL_DIRECTIONS) == expected


def test_vnx_routes_need_bridge():
    for r in ALL_ROUTES:
        if "vnx" in (r.buy_chain, r.sell_chain):
            assert r.needs_bridge


def test_route_fees_vnx_platform():
    cfg = load_bot_config()
    fee = estimate_fees_usd("base", "vnx", cfg)
    assert fee > cfg.vnx_platform_fee_usd


def test_route_fees_ethereum_hub():
    cfg = load_bot_config()
    fee = estimate_fees_usd("ethereum", "vnx", cfg)
    assert fee > cfg.vnx_platform_fee_usd + cfg.eth_gas_usd_estimate


def test_base_sol_bridge_fee():
    cfg = load_bot_config()
    fee = estimate_fees_usd("base", "solana", cfg)
    assert fee >= cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd


def test_vnx_arb_enabled_by_default():
    from src.scanner.routes import active_directions

    cfg = load_bot_config()
    active = set(active_directions(cfg))
    assert active == {
        "base_to_solana",
        "solana_to_base",
        "base_to_vnx",
        "vnx_to_base",
        "solana_to_vnx",
        "vnx_to_solana",
        "ethereum_to_vnx",
        "vnx_to_ethereum",
    }


def test_active_routes_respects_env(monkeypatch):
    from src.config_loader import load_bot_config
    from src.scanner.routes import active_directions

    monkeypatch.setenv("ENABLE_VNX_ARB_ROUTES", "false")
    cfg = load_bot_config()
    active = set(active_directions(cfg))
    assert "base_to_vnx" not in active
    assert "vnx_to_base" not in active
    assert "ethereum_to_vnx" not in active
    assert "vnx_to_ethereum" not in active
    assert len(active) == 4
