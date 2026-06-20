from src.config_loader import BotConfig
from src.scanner.routes import active_directions
from src.scanner.selector import RouteGroupBest, choose_execution
from src.scanner.simulator import CycleSimulation


def _sim(direction: str, net: float) -> CycleSimulation:
    parts = direction.split("_to_")
    return CycleSimulation(
        direction=direction,
        buy_chain=parts[0],
        sell_chain=parts[1],
        size_vchf=500,
        stable_in_usd=700,
        stable_out_usd=700 + net,
        token_mid=500,
        net_profit_usd=net,
        profitable=net > 0,
        sanity_ok=True,
    )


def _best(group: str, direction: str, net: float) -> RouteGroupBest:
    return RouteGroupBest(group, direction, 500, net, _sim(direction, net))


def test_only_celo_sol():
    cfg = BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[50],
        slippage_bps=50,
        quote_freshness_sec=30,
        peg_min=0.98,
        peg_max=1.02,
        vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600,
        base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0,
        vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=False,
        enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0,
        cctp_fee_usd=1.5,
    )
    r = choose_execution(_best("celo_sol", "celo_to_solana", 10), None, cfg)
    assert r.opportunity.direction == "celo_to_solana"


def test_indirect_when_premium_met():
    cfg = BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[50],
        slippage_bps=50,
        quote_freshness_sec=30,
        peg_min=0.98,
        peg_max=1.02,
        vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600,
        base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0,
        vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=False,
        enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0,
        cctp_fee_usd=1.5,
    )
    cs = _best("celo_sol", "celo_to_solana", 10)
    vs = _best("vnx_sol", "vnx_to_solana", 16)
    r = choose_execution(cs, vs, cfg)
    assert r.opportunity.direction == "vnx_to_solana"
    assert "premium" in r.reason


def test_prefer_celo_sol_when_close():
    cfg = BotConfig(
        poll_interval_sec=60,
        min_profit_usd=5,
        min_trade_vchf=200,
        max_trade_vchf=2000,
        sizing_coarse_step=100,
        max_sizing_quotes=5,
        probe_sizes=[50],
        slippage_bps=50,
        quote_freshness_sec=30,
        peg_min=0.98,
        peg_max=1.02,
        vnx_bridge_poll_sec=30,
        vnx_bridge_timeout_sec=3600,
        base_gas_usd_estimate=0.25,
        solana_fee_usd_estimate=0.05,
        vnx_bridge_fee_usd=1.0,
        vnx_platform_fee_usd=0.5,
        wormhole_bridge_fee_usd=0.5,
        enable_vnx_arb_routes=False,
        enable_vnx_cctp_routes=True,
        indirect_route_premium_usd=5.0,
        eth_gas_usd_estimate=2.0,
        cctp_fee_usd=1.5,
    )
    cs = _best("celo_sol", "solana_to_celo", 12)
    vs = _best("vnx_sol", "solana_to_vnx", 14)
    r = choose_execution(cs, vs, cfg)
    assert r.opportunity.direction == "solana_to_celo"


def test_cctp_routes_active_by_default():
    from src.config_loader import load_bot_config

    cfg = load_bot_config()
    active = set(active_directions(cfg))
    assert "celo_to_solana" in active
    assert "solana_to_vnx" in active
    assert "vnx_to_solana" in active
    assert "celo_to_vnx" in active
    assert "vnx_to_celo" in active
