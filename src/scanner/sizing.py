from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.scanner.simulator import (
    CycleSimulation,
    RoundTripSimulation,
    simulate_direction,
    simulate_round_trip,
)
from src.treasury.loops import origin_for_direction

logger = logging.getLogger(__name__)


@dataclass
class SizedSimulation:
    size_vchf: float
    simulation: CycleSimulation
    round_trip_profit_usd: float | None = None
    round_trip: RoundTripSimulation | None = None


def _qualifies_one_way(sim: CycleSimulation, cfg: BotConfig) -> bool:
    return (
        sim.error is None
        and sim.sanity_ok
        and sim.net_profit_usd >= cfg.min_profit_usd
    )


def _qualifies_round_trip(rt: RoundTripSimulation, cfg: BotConfig) -> bool:
    if rt.primary.error and not rt.primary.profitable:
        return False
    if not rt.primary.sanity_ok:
        return False
    if rt.return_sim is not None and not rt.return_sim.sanity_ok:
        return False
    return rt.profitable and rt.round_trip_profit_usd >= cfg.min_profit_usd


def _qualifies(sim: CycleSimulation, cfg: BotConfig) -> bool:
    return _qualifies_one_way(sim, cfg)


async def search_profitable_size(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
    direction: str,
) -> SizedSimulation | None:
    """
    Light binary search between min_trade_vchf and max_trade_vchf.

    At most max_sizing_quotes simulations per direction:
      1) endpoints (min, max), or single point when min == max
      2) midpoint while range > sizing_coarse_step

    Probes interior sizes even when both endpoints fail min_profit — slippage
    curves can have a profitable sweet spot between min and max.
    """
    lo = cfg.min_trade_vchf
    hi = cfg.max_trade_vchf
    if lo > hi:
        logger.warning("invalid sizing range lo=%s hi=%s", lo, hi)
        return None

    close_loop = cfg.close_loop_after_cycle
    quotes_used = 0
    best: SizedSimulation | None = None
    cache: dict[float, CycleSimulation | RoundTripSimulation] = {}

    async def sim_at(size: float) -> CycleSimulation | RoundTripSimulation:
        nonlocal quotes_used
        size = round(size, 2)
        if size not in cache:
            if close_loop:
                cache[size] = await simulate_round_trip(
                    client,
                    chains,
                    token,
                    cfg,
                    direction,
                    size,
                    origin=origin_for_direction(direction),
                )
            else:
                cache[size] = await simulate_direction(
                    client, chains, token, cfg, direction, size
                )
            quotes_used += 1
        return cache[size]

    def profit_at(result: CycleSimulation | RoundTripSimulation) -> float:
        if isinstance(result, RoundTripSimulation):
            return result.round_trip_profit_usd
        return result.net_profit_usd

    def primary_sim(result: CycleSimulation | RoundTripSimulation) -> CycleSimulation:
        if isinstance(result, RoundTripSimulation):
            return result.primary
        return result

    def qualifies_at(result: CycleSimulation | RoundTripSimulation) -> bool:
        if isinstance(result, RoundTripSimulation):
            return _qualifies_round_trip(result, cfg)
        return _qualifies_one_way(result, cfg)

    if lo == hi:
        result = await sim_at(lo)
        if not qualifies_at(result):
            return None
        sim = primary_sim(result)
        rt_profit = profit_at(result) if close_loop else None
        rt = result if isinstance(result, RoundTripSimulation) else None
        return SizedSimulation(lo, sim, rt_profit, rt)

    def consider(size: float, result: CycleSimulation | RoundTripSimulation) -> None:
        nonlocal best
        if not qualifies_at(result):
            return
        p = profit_at(result)
        sim = primary_sim(result)
        rt = result if isinstance(result, RoundTripSimulation) else None
        if best is None or p > (best.round_trip_profit_usd or best.simulation.net_profit_usd):
            best = SizedSimulation(size, sim, p if close_loop else None, rt)
        elif (
            best is not None
            and p == (best.round_trip_profit_usd or best.simulation.net_profit_usd)
            and size > best.size_vchf
        ):
            best = SizedSimulation(size, sim, p if close_loop else None, rt)

    sim_lo = await sim_at(lo)
    sim_hi = await sim_at(hi)
    consider(lo, sim_lo)
    consider(hi, sim_hi)

    if quotes_used >= cfg.max_sizing_quotes:
        return best

    left, right = lo, hi
    while right - left > cfg.sizing_coarse_step and quotes_used < cfg.max_sizing_quotes:
        mid = round((left + right) / 2, 0)
        if mid <= left or mid >= right:
            break
        sim_mid = await sim_at(mid)
        consider(mid, sim_mid)
        ps = primary_sim(sim_mid)
        if ps.error:
            break
        if profit_at(sim_mid) >= cfg.min_profit_usd:
            left = mid
        else:
            right = mid

    logger.debug("%s sizing used %d quotes, best=%s", direction, quotes_used, best)
    return best


def cap_sizes(sizes: list[float], max_vchf: float | None = None) -> list[float]:
    """Legacy helper for test/report scripts."""
    if max_vchf is None:
        from src.config_loader import load_bot_config

        max_vchf = load_bot_config().max_trade_vchf
    return [s for s in sizes if s <= max_vchf]
