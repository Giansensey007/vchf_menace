from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from src.config_loader import BotConfig, ChainConfig, TokenConfig, token_decimals
from src.bridge.hub_usdt import (
    normalize_hub_to_usdt,
    usdc_raw_for_solana_buy,
    usdt_raw_for_celo_buy,
)
from src.quotes.router import buy_token_with_stable, sell_token_for_stable
from src.quotes.sanity import check_stable_peg, check_vchf_usd_rate
from src.quotes.types import from_human, to_human
from src.scanner.routes import (
    ALL_ROUTES,
    CCTP_SOL_USDC_TO_VNX,
    RouteSpec,
    active_routes,
    estimate_cctp_usdc_return_fees,
    estimate_fees_usd,
    route_for_direction,
)

logger = logging.getLogger(__name__)

VNX_MIN_VCHF = 30.0


def _is_vnx_route(buy_key: str, sell_key: str) -> bool:
    return "vnx" in (buy_key, sell_key)


async def _stable_cost_to_buy_vchf(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    chain_key: str,
    size_vchf: float,
) -> tuple[float, float, object | None]:
    """
    USDC cost to acquire `size_vchf` on chain_key (matches executor fixed-size legs).
    Returns (stable_in_usd, token_mid, buy_quote).
    """
    chain_cfg = chains[chain_key]
    dec = token_decimals(token, chain_key)
    amount_in = from_human(size_vchf, dec)

    if chain_key == "vnx":
        from src.quotes import vnx as vnx_mod

        quotes = await vnx_mod._load_quotes(client)
        q = quotes.get("VCHF/USDC")
        if not q:
            return 0.0, 0.0, None
        ask_price, _ = vnx_mod._price_and_liq(q.get("a"))
        if ask_price <= 0:
            return 0.0, 0.0, None
        stable_in = size_vchf * ask_price
        buy_raw = from_human(stable_in * 1.002, chain_cfg.hub_decimals)
        buy_q = await buy_token_with_stable(client, chain_cfg, token, chain_key, buy_raw)
        if not buy_q:
            return stable_in, 0.0, None
        token_mid = float(to_human(buy_q.amount_out, dec))
        return stable_in, token_mid, buy_q

    sell_hint = await sell_token_for_stable(client, chain_cfg, token, chain_key, amount_in)
    if not sell_hint:
        return 0.0, 0.0, None
    stable_in = float(to_human(sell_hint.amount_out, chain_cfg.hub_decimals))
    buy_raw = from_human(stable_in * 1.01, chain_cfg.hub_decimals)
    buy_q = await buy_token_with_stable(client, chain_cfg, token, chain_key, buy_raw)
    if not buy_q:
        return stable_in, size_vchf, None
    got = float(to_human(buy_q.amount_out, dec))
    if got > 0 and got < size_vchf * 0.999:
        stable_in = stable_in * (size_vchf / got)
    return stable_in, size_vchf, buy_q


async def _simulate_fixed_size_vnx_route(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    route: RouteSpec,
    size_vchf: float,
) -> CycleSimulation:
    """Buy size_vchf on buy_chain, sell size_vchf on sell_chain — matches executor economics."""
    direction = route.direction
    buy_key, sell_key = route.buy_chain, route.sell_chain
    buy_chain = chains[buy_key]
    sell_chain = chains[sell_key]
    sell_dec = token_decimals(token, sell_key)
    notes: list[str] = []

    stable_in, token_mid, buy_q = await _stable_cost_to_buy_vchf(
        client, chains, token, buy_key, size_vchf
    )
    if not buy_q:
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=stable_in,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            needs_bridge=route.needs_bridge,
            error=f"no buy quote on {buy_key}",
        )

    sell_q = await sell_token_for_stable(
        client, sell_chain, token, sell_key, from_human(size_vchf, sell_dec)
    )
    if not sell_q:
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=stable_in,
            stable_out_usd=0,
            token_mid=token_mid,
            net_profit_usd=0,
            profitable=False,
            needs_bridge=route.needs_bridge,
            error=f"no sell quote on {sell_key}",
        )

    stable_out = float(to_human(sell_q.amount_out, sell_chain.hub_decimals))

    ok_rate_out, msg_out = check_vchf_usd_rate(stable_out, size_vchf, cfg)
    ok_rate_in, msg_in = check_vchf_usd_rate(stable_in, size_vchf, cfg)
    if not ok_rate_out:
        notes.append(msg_out)
    if not ok_rate_in:
        notes.append(msg_in)

    fees = estimate_fees_usd(buy_key, sell_key, cfg)
    net = stable_out - stable_in - fees
    sanity_ok = ok_rate_out and ok_rate_in

    return CycleSimulation(
        direction=direction,
        buy_chain=buy_key,
        sell_chain=sell_key,
        size_vchf=size_vchf,
        stable_in_usd=stable_in,
        stable_out_usd=stable_out,
        token_mid=token_mid if token_mid > 0 else size_vchf,
        net_profit_usd=net,
        profitable=net > 0 and sanity_ok,
        needs_bridge=route.needs_bridge,
        leg1={"chain": buy_key, "action": "buy_vchf", "provider": buy_q.provider},
        leg2={"chain": sell_key, "action": "sell_vchf", "provider": sell_q.provider},
        fees_usd=fees,
        sanity_ok=sanity_ok,
        sanity_notes=notes,
    )


async def simulate_cctp_usdc_return_to_vnx(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    usdc_on_sol: float,
    target_vchf: float,
) -> CycleSimulation:
    """
    Return leg: Sol USDC → CCTP → ETH USDC → VNX deposit → platform VCHF buy.
    stable_in = USDC leaving Sol; stable_out = bid value of VCHF received on platform.
    """
    from src.bridge.cctp import CircleCctpBridge
    from src.quotes import vnx as vnx_mod
    from src.vnx.deposits import min_deposit_usdc

    direction = CCTP_SOL_USDC_TO_VNX
    notes: list[str] = []

    if usdc_on_sol <= 0:
        return CycleSimulation(
            direction=direction,
            buy_chain="vnx",
            sell_chain="solana",
            size_vchf=target_vchf,
            stable_in_usd=0,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error="zero USDC on Sol for CCTP return",
        )

    cctp = CircleCctpBridge()
    cq = await cctp.quote_usdc(client, "solana", "ethereum", usdc_on_sol)
    if not cq.ok:
        return CycleSimulation(
            direction=direction,
            buy_chain="vnx",
            sell_chain="solana",
            size_vchf=target_vchf,
            stable_in_usd=usdc_on_sol,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error=cq.error or "CCTP quote failed",
        )

    usdc_on_vnx = cq.amount_out_usdc
    dep_min = min_deposit_usdc("ETH")
    if usdc_on_vnx < dep_min:
        return CycleSimulation(
            direction=direction,
            buy_chain="vnx",
            sell_chain="solana",
            size_vchf=target_vchf,
            stable_in_usd=usdc_on_sol,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error=f"USDC after CCTP ${usdc_on_vnx:.2f} below ETH deposit min ${dep_min:.2f}",
        )

    buy_raw = from_human(usdc_on_vnx, 6)
    buy_q = await buy_token_with_stable(client, chains["vnx"], token, "vnx", buy_raw)
    if not buy_q:
        return CycleSimulation(
            direction=direction,
            buy_chain="vnx",
            sell_chain="solana",
            size_vchf=target_vchf,
            stable_in_usd=usdc_on_sol,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error="no VNX platform buy quote for CCTP return",
        )

    vnx_dec = token_decimals(token, "vnx")
    vchf_out = float(to_human(buy_q.amount_out, vnx_dec))

    quotes = await vnx_mod._load_quotes(client)
    q = quotes.get("VCHF/USDC", {})
    bid_price, _ = vnx_mod._price_and_liq(q.get("b"))
    stable_out = vchf_out * bid_price if bid_price > 0 else 0.0

    fees = cfg.eth_gas_usd_estimate + cfg.vnx_platform_fee_usd
    net = stable_out - usdc_on_sol - fees

    ok_rate, rate_msg = check_vchf_usd_rate(stable_out, vchf_out, cfg)
    if not ok_rate:
        notes.append(rate_msg)
    notes.append(f"CCTP fee ${cq.fee_usd:.2f} → ${usdc_on_vnx:.2f} USDC on VNX")

    return CycleSimulation(
        direction=direction,
        buy_chain="vnx",
        sell_chain="solana",
        size_vchf=target_vchf,
        stable_in_usd=usdc_on_sol,
        stable_out_usd=stable_out,
        token_mid=vchf_out,
        net_profit_usd=net,
        profitable=net > 0 and ok_rate,
        needs_bridge=True,
        leg1={"chain": "solana", "action": "cctp_usdc_to_eth", "provider": "cctp"},
        leg2={"chain": "vnx", "action": "buy_vchf", "provider": buy_q.provider},
        fees_usd=fees + cq.fee_usd,
        sanity_ok=ok_rate,
        sanity_notes=notes,
    )

@dataclass
class CycleSimulation:
    direction: str
    buy_chain: str
    sell_chain: str
    size_vchf: float
    stable_in_usd: float
    stable_out_usd: float
    token_mid: float
    net_profit_usd: float
    profitable: bool
    needs_bridge: bool = False
    leg1: dict = field(default_factory=dict)
    leg2: dict = field(default_factory=dict)
    fees_usd: float = 0.0
    sanity_ok: bool = True
    sanity_notes: list[str] = field(default_factory=list)
    error: str | None = None


async def simulate_route(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    route: RouteSpec,
    size_vchf: float,
) -> CycleSimulation:
    direction = route.direction
    buy_key, sell_key = route.buy_chain, route.sell_chain

    if size_vchf < VNX_MIN_VCHF and ("vnx" in (buy_key, sell_key)):
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=0,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            needs_bridge=route.needs_bridge,
            error=f"below VNX min order ({VNX_MIN_VCHF} VCHF)",
        )

    if _is_vnx_route(buy_key, sell_key):
        return await _simulate_fixed_size_vnx_route(client, chains, token, cfg, route, size_vchf)

    if buy_key not in chains or sell_key not in chains:
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=0,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error=f"chain not loaded: {buy_key}/{sell_key}",
        )

    buy_chain = chains[buy_key]
    sell_chain = chains[sell_key]
    buy_dec = token_decimals(token, buy_key)
    sell_dec = token_decimals(token, sell_key)
    notes: list[str] = []

    def _is_celo_sol_route(bk: str, sk: str) -> bool:
        return {bk, sk} == {"celo", "solana"}

    vchf_sell_amt = from_human(size_vchf, sell_dec)
    sell_q = await sell_token_for_stable(client, sell_chain, token, sell_key, vchf_sell_amt)
    if not sell_q:
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=0,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            needs_bridge=route.needs_bridge,
            error=f"no sell quote on {sell_key}",
        )

    stable_out = float(to_human(sell_q.amount_out, sell_chain.hub_decimals))
    if _is_celo_sol_route(buy_key, sell_key):
        stable_out = await normalize_hub_to_usdt(client, sell_key, sell_chain, sell_q.amount_out)

    ok_rate, rate_msg = check_vchf_usd_rate(stable_out, size_vchf, cfg)
    if not ok_rate:
        notes.append(rate_msg)

    if _is_celo_sol_route(buy_key, sell_key):
        if buy_key == "celo":
            stable_in_raw = usdt_raw_for_celo_buy(stable_out)
        else:
            usdc_raw, conv_err = await usdc_raw_for_solana_buy(client, stable_out)
            stable_in_raw = usdc_raw if usdc_raw is not None else from_human(stable_out, buy_chain.hub_decimals)
            if conv_err:
                notes.append(f"USDT→USDC: {conv_err}")
    else:
        stable_in_raw = from_human(stable_out, buy_chain.hub_decimals)
    buy_q = await buy_token_with_stable(client, buy_chain, token, buy_key, stable_in_raw)
    if not buy_q:
        return CycleSimulation(
            direction=direction,
            buy_chain=buy_key,
            sell_chain=sell_key,
            size_vchf=size_vchf,
            stable_in_usd=0,
            stable_out_usd=stable_out,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            needs_bridge=route.needs_bridge,
            error=f"no buy quote on {buy_key}",
        )

    vchf_bought = float(to_human(buy_q.amount_out, buy_dec))
    if _is_celo_sol_route(buy_key, sell_key):
        stable_in = await normalize_hub_to_usdt(client, buy_key, buy_chain, stable_in_raw)
    else:
        stable_in = float(to_human(stable_in_raw, buy_chain.hub_decimals))

    ok_peg, peg_msg = check_stable_peg(stable_in, stable_out, cfg)
    if not ok_peg:
        notes.append(peg_msg)

    fees = estimate_fees_usd(buy_key, sell_key, cfg)
    net = stable_out - stable_in - fees
    sanity_ok = ok_rate and ok_peg

    return CycleSimulation(
        direction=direction,
        buy_chain=buy_key,
        sell_chain=sell_key,
        size_vchf=size_vchf,
        stable_in_usd=stable_in,
        stable_out_usd=stable_out,
        token_mid=vchf_bought,
        net_profit_usd=net,
        profitable=net > 0 and sanity_ok,
        needs_bridge=route.needs_bridge,
        leg1={"chain": buy_key, "action": "buy_vchf", "provider": buy_q.provider},
        leg2={"chain": sell_key, "action": "sell_vchf", "provider": sell_q.provider},
        fees_usd=fees,
        sanity_ok=sanity_ok,
        sanity_notes=notes,
    )


async def simulate_direction(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    direction: str,
    size_vchf: float,
) -> CycleSimulation:
    route = route_for_direction(direction)
    if not route:
        return CycleSimulation(
            direction=direction,
            buy_chain="",
            sell_chain="",
            size_vchf=size_vchf,
            stable_in_usd=0,
            stable_out_usd=0,
            token_mid=0,
            net_profit_usd=0,
            profitable=False,
            error="unknown direction",
        )
    return await simulate_route(client, chains, token, cfg, route, size_vchf)


async def simulate_all_routes(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    size_vchf: float,
) -> list[CycleSimulation]:
    results: list[CycleSimulation] = []
    for route in active_routes(cfg):
        results.append(await simulate_route(client, chains, token, cfg, route, size_vchf))
    return results


@dataclass
class RoundTripSimulation:
    origin: str
    direction: str
    primary: CycleSimulation
    return_direction: str | None
    return_sim: CycleSimulation | None
    round_trip_profit_usd: float
    profitable: bool
    closed: bool
    error: str | None = None


async def simulate_round_trip(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    direction: str,
    size_vchf: float,
    *,
    origin: str | None = None,
) -> RoundTripSimulation:
    from src.treasury.loops import (
        closes_to_origin,
        origin_for_direction,
        return_closes_origin_with_cctp,
        return_leg_direction,
        use_cctp_usdc_return,
    )

    hub = origin or origin_for_direction(direction)
    enable_cctp = cfg.enable_vnx_cctp_routes
    primary = await simulate_direction(client, chains, token, cfg, direction, size_vchf)
    if primary.error and not primary.profitable:
        return RoundTripSimulation(
            hub, direction, primary, None, None, 0.0, False, False, primary.error
        )
    if closes_to_origin(hub, direction):
        return RoundTripSimulation(
            hub,
            direction,
            primary,
            None,
            None,
            primary.net_profit_usd,
            primary.profitable,
            True,
        )
    inv = return_leg_direction(hub, direction, enable_cctp=enable_cctp)
    if not inv or not return_closes_origin_with_cctp(hub, direction, enable_cctp=enable_cctp):
        return RoundTripSimulation(
            hub,
            direction,
            primary,
            inv,
            None,
            primary.net_profit_usd,
            False,
            False,
            f"no return path to {hub}",
        )
    return_size = size_vchf
    if primary.token_mid > 0:
        return_size = primary.token_mid

    if use_cctp_usdc_return(hub, direction, enable_cctp=enable_cctp):
        usdc_on_sol = primary.stable_out_usd
        ret = await simulate_cctp_usdc_return_to_vnx(
            client, chains, token, cfg, usdc_on_sol, return_size
        )
    else:
        ret = await simulate_direction(client, chains, token, cfg, inv, return_size)
    round_p = primary.net_profit_usd + ret.net_profit_usd
    ok = round_p >= cfg.min_profit_usd and primary.sanity_ok and ret.sanity_ok
    return RoundTripSimulation(
        hub,
        direction,
        primary,
        inv,
        ret,
        round_p,
        ok,
        ret.profitable or cfg.close_loop_always_return,
        ret.error,
    )
