"""Quote/size the platform-first same-asset loops (Loop 1/2/3).

Each loop starts with `size` units of the VNX token on the platform and ends with
`token_out` units back on the platform. Profit is the token gained, valued at the
platform bid. Every leg's fee and floor (platform min order, ETH deposit minimum)
is accounted; the on-chain/platform buy-back legs quote with ``is_buyback=True``.

This is additive — it does not replace the legacy directed-route simulator yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from src.config_loader import BotConfig, ChainConfig, TokenConfig, token_decimals
from src.quotes.router import buy_token_with_stable, sell_token_for_stable
from src.quotes.types import from_human, to_human
from src.scanner.routes import (
    HUB_CHAIN,
    LOOP1_OUTBOUND,
    LOOP2_INBOUND,
    LOOP3_CROSS,
    LoopSpec,
)
from src.vnx.deposits import min_deposit_usdc
from src.vnx.trading import VCHF_MIN_ORDER

logger = logging.getLogger(__name__)

MIN_ORDER = VCHF_MIN_ORDER


@dataclass
class LoopLeg:
    kind: str
    venue: str
    detail: str
    usd_after: float = 0.0
    fee_usd: float = 0.0
    provider: str = ""


@dataclass
class LoopSimulation:
    loop_key: str
    family: str
    token: str
    size: float
    token_out: float = 0.0
    net_token: float = 0.0
    net_profit_usd: float = 0.0
    ref_price: float = 0.0
    fees_usd: float = 0.0
    profitable: bool = False
    floors_ok: bool = True
    legs: list[LoopLeg] = field(default_factory=list)
    error: str | None = None


def _chain_gas(chain_key: str, cfg: BotConfig) -> float:
    if chain_key == "celo":
        return cfg.celo_gas_usd_estimate
    if chain_key == "solana":
        return cfg.solana_fee_usd_estimate
    if chain_key == "base":
        return float(getattr(cfg, "base_gas_usd_estimate", 0.25))
    if chain_key == "ethereum":
        return cfg.eth_gas_usd_estimate
    return 0.0


async def _platform_ref_price(client: httpx.AsyncClient, token_symbol: str) -> tuple[float, float]:
    """(bid, ask) for token/USDC on the VNX platform; (0, 0) if unavailable."""
    from src.quotes import vnx as vnx_mod

    quotes = await vnx_mod._load_quotes(client)
    q = quotes.get(f"{token_symbol}/USDC", {})
    bid, _ = vnx_mod._price_and_liq(q.get("b"))
    ask, _ = vnx_mod._price_and_liq(q.get("a"))
    return bid, ask


async def _bridge_fee_usd(
    client: httpx.AsyncClient, mechanism: str | None, frm: str, to: str, amount_usd: float, cfg: BotConfig
) -> float:
    if mechanism in (None, "none"):
        return 0.0
    if mechanism == "cctp":
        from src.bridge.cctp import CircleCctpBridge

        cq = await CircleCctpBridge().quote_usdc(client, frm, to, amount_usd)
        return cq.fee_usd if cq.ok else cfg.cctp_fee_usd
    if mechanism == "wormhole":
        return cfg.wormhole_bridge_fee_usd
    # eth_triangle (e.g. Celo<->Base): Wormhole leg + CCTP leg
    fee = cfg.wormhole_bridge_fee_usd
    evm = "base" if "base" in (frm, to) else (frm if frm != "celo" else to)
    try:
        from src.bridge.cctp import CircleCctpBridge

        cq = await CircleCctpBridge().quote_usdc(client, HUB_CHAIN, evm, amount_usd)
        fee += cq.fee_usd if cq.ok else cfg.cctp_fee_usd
    except Exception:
        fee += cfg.cctp_fee_usd
    return fee


async def _sell_onchain_usd(
    client: httpx.AsyncClient, chains: dict[str, ChainConfig], token: TokenConfig, chain_key: str, size: float
) -> tuple[float, str]:
    chain = chains[chain_key]
    dec = token_decimals(token, chain_key)
    q = await sell_token_for_stable(client, chain, token, chain_key, from_human(size, dec))
    if not q:
        return 0.0, ""
    return float(to_human(q.amount_out, chain.hub_decimals)), q.provider


async def _buyback_token(
    client: httpx.AsyncClient, chains: dict[str, ChainConfig], token: TokenConfig, chain_key: str, usd: float
) -> tuple[float, str]:
    if usd <= 0:
        return 0.0, ""
    chain = chains[chain_key]
    dec = token_decimals(token, chain_key)
    q = await buy_token_with_stable(
        client, chain, token, chain_key, from_human(usd, chain.hub_decimals), is_buyback=True
    )
    if not q:
        return 0.0, ""
    return float(to_human(q.amount_out, dec)), q.provider


def _finish(sim: LoopSimulation, size: float, token_out: float, ref_bid: float, cfg: BotConfig) -> LoopSimulation:
    sim.token_out = token_out
    sim.net_token = token_out - size
    sim.net_profit_usd = sim.net_token * ref_bid
    sim.ref_price = ref_bid
    sim.profitable = sim.floors_ok and sim.net_profit_usd >= cfg.min_profit_usd
    return sim


async def simulate_loop(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    loop: LoopSpec,
    size: float,
) -> LoopSimulation:
    sim = LoopSimulation(loop_key=loop.key, family=loop.family, token=token.symbol, size=size)

    if size < MIN_ORDER:
        sim.floors_ok = False
        sim.error = f"size {size} below platform min order ({MIN_ORDER})"
        return sim

    ref_bid, ref_ask = await _platform_ref_price(client, token.symbol)
    if ref_bid <= 0:
        sim.error = "no platform reference price"
        return sim

    dep_min = min_deposit_usdc("ETH")

    if loop.family == LOOP1_OUTBOUND:
        x = loop.chain_a
        sell_usd, prov = await _sell_onchain_usd(client, chains, token, x, size)
        if sell_usd <= 0:
            sim.error = f"no sell quote on {x}"
            return sim
        fees = _chain_gas(x, cfg)
        usd = sell_usd - _chain_gas(x, cfg)
        sim.legs.append(LoopLeg("sell_onchain", x, f"sell {token.symbol} on {x}", usd, _chain_gas(x, cfg), prov))
        if x != loop.hub:
            mech = next((s.mechanism for s in loop.bridge_legs), None)
            bf = await _bridge_fee_usd(client, mech, x, loop.hub, usd, cfg)
            fees += bf
            usd -= bf
            sim.legs.append(LoopLeg("bridge_stable", x, f"bridge {x}->{loop.hub} ({mech})", usd, bf))
        if usd < dep_min:
            sim.floors_ok = False
            sim.error = f"USDC ${usd:.2f} below ETH deposit min ${dep_min:.2f}"
            sim.fees_usd = fees
            return sim
        usd -= cfg.eth_gas_usd_estimate + cfg.vnx_bridge_fee_usd + cfg.vnx_platform_fee_usd
        fees += cfg.eth_gas_usd_estimate + cfg.vnx_bridge_fee_usd + cfg.vnx_platform_fee_usd
        sim.legs.append(LoopLeg("vnx_usdc_deposit", loop.hub, "deposit USDC -> platform", usd))
        token_out, bprov = await _buyback_token(client, chains, token, "vnx", usd)
        sim.legs.append(LoopLeg("platform_buyback", "vnx", f"buy-back {token.symbol}", usd, 0.0, bprov))
        sim.fees_usd = fees
        return _finish(sim, size, token_out, ref_bid, cfg)

    if loop.family == LOOP2_INBOUND:
        x = loop.chain_a
        sell_usd, prov = await _sell_onchain_usd(client, chains, token, "vnx", size)
        if sell_usd <= 0:
            sim.error = "no platform sell quote"
            return sim
        fees = cfg.vnx_platform_fee_usd + cfg.eth_gas_usd_estimate + cfg.vnx_bridge_fee_usd
        usd = sell_usd - fees
        sim.legs.append(LoopLeg("platform_sell", "vnx", f"sell {token.symbol} for USDC", usd, fees, prov))
        if x != loop.hub:
            mech = next((s.mechanism for s in loop.bridge_legs), None)
            bf = await _bridge_fee_usd(client, mech, loop.hub, x, usd, cfg)
            fees += bf
            usd -= bf
            sim.legs.append(LoopLeg("bridge_stable", loop.hub, f"bridge {loop.hub}->{x} ({mech})", usd, bf))
        usd -= _chain_gas(x, cfg)
        fees += _chain_gas(x, cfg) + cfg.vnx_bridge_fee_usd  # buyback gas + token deposit
        token_out, bprov = await _buyback_token(client, chains, token, x, usd)
        sim.legs.append(LoopLeg("onchain_buyback", x, f"buy-back {token.symbol} on {x}", usd, 0.0, bprov))
        sim.legs.append(LoopLeg("vnx_token_deposit", x, f"deposit {token.symbol} -> platform", usd))
        sim.fees_usd = fees
        return _finish(sim, size, token_out, ref_bid, cfg)

    if loop.family == LOOP3_CROSS:
        a, b = loop.chain_a, loop.chain_b
        assert b is not None
        sell_usd, prov = await _sell_onchain_usd(client, chains, token, a, size)
        if sell_usd <= 0:
            sim.error = f"no sell quote on {a}"
            return sim
        fees = _chain_gas(a, cfg)
        usd = sell_usd - _chain_gas(a, cfg)
        sim.legs.append(LoopLeg("sell_onchain", a, f"sell {token.symbol} on {a}", usd, _chain_gas(a, cfg), prov))
        mech = next((s.mechanism for s in loop.bridge_legs), None)
        bf = await _bridge_fee_usd(client, mech, a, b, usd, cfg)
        fees += bf
        usd -= bf
        sim.legs.append(LoopLeg("bridge_stable", a, f"bridge {a}->{b} ({mech})", usd, bf))
        usd -= _chain_gas(b, cfg)
        fees += _chain_gas(b, cfg) + cfg.vnx_bridge_fee_usd
        token_out, bprov = await _buyback_token(client, chains, token, b, usd)
        sim.legs.append(LoopLeg("onchain_buyback", b, f"buy-back {token.symbol} on {b}", usd, 0.0, bprov))
        sim.legs.append(LoopLeg("vnx_token_deposit", b, f"deposit {token.symbol} -> platform", usd))
        sim.fees_usd = fees
        return _finish(sim, size, token_out, ref_bid, cfg)

    sim.error = f"unknown loop family: {loop.family}"
    return sim


async def simulate_all_loops(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    size: float,
) -> list[LoopSimulation]:
    from src.scanner.routes import active_loops

    return [await simulate_loop(client, chains, token, cfg, loop, size) for loop in active_loops(cfg, token)]
