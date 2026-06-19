from src.scanner.routes import ALL_DIRECTIONS, ALL_ROUTES, RouteSpec, estimate_fees_usd
from src.config_loader import load_bot_config


def test_all_six_directions():
    assert len(ALL_ROUTES) == 6
    assert len(ALL_DIRECTIONS) == 6
    expected = {
        "celo_to_solana",
        "solana_to_celo",
        "celo_to_vnx",
        "vnx_to_celo",
        "solana_to_vnx",
        "vnx_to_solana",
    }
    assert set(ALL_DIRECTIONS) == expected


def test_vnx_routes_need_bridge():
    for r in ALL_ROUTES:
        if "vnx" in (r.buy_chain, r.sell_chain):
            assert r.needs_bridge


def test_route_fees_vnx_platform():
    cfg = load_bot_config()
    fee = estimate_fees_usd("celo", "vnx", cfg)
    assert fee > cfg.vnx_platform_fee_usd


def test_celo_sol_bridge_fee():
    cfg = load_bot_config()
    fee = estimate_fees_usd("celo", "solana", cfg)
    assert fee >= cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd


def test_vnx_arb_enabled_by_default():
    from src.scanner.routes import active_directions

    cfg = load_bot_config()
    active = set(active_directions(cfg))
    assert active == {
        "celo_to_solana",
        "solana_to_celo",
        "celo_to_vnx",
        "vnx_to_celo",
        "solana_to_vnx",
        "vnx_to_solana",
    }


def test_active_routes_respects_env(monkeypatch):
    from src.config_loader import load_bot_config
    from src.scanner.routes import active_directions

    monkeypatch.setenv("ENABLE_VNX_ARB_ROUTES", "false")
    cfg = load_bot_config()
    active = set(active_directions(cfg))
    assert "celo_to_vnx" not in active
    assert "vnx_to_celo" not in active
    assert len(active) == 4
