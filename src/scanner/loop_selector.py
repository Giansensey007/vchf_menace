"""Pick the best same-asset loop to execute this cycle.

Scans every active loop (Loop 1/2/3) with the loop simulator and returns the
most profitable one that clears the platform/deposit floors and the bot's
``min_profit_usd``. Two-phase to bound quote volume:

1. Probe all loops once at a representative size to find the best loop family.
2. Size-search that single loop across the trade grid for the best size.

Selection is read-only (quotes only). Execution is handled by
``src.execution.loop_executor.LoopExecutor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from src.config_loader import BotConfig, ChainConfig, TokenConfig
from src.scanner.loop_simulator import LoopSimulation, simulate_loop
from src.scanner.routes import active_loops

logger = logging.getLogger(__name__)


@dataclass
class LoopSelection:
    best: LoopSimulation | None = None
    reason: str = ""
    candidates: list[LoopSimulation] = field(default_factory=list)


def _trade_bounds(cfg: BotConfig) -> tuple[float, float]:
    lo = next(
        (getattr(cfg, f) for f in ("min_trade_vgbp", "min_trade_vchf", "min_trade_vnxau") if hasattr(cfg, f)),
        100.0,
    )
    hi = next(
        (getattr(cfg, f) for f in ("max_trade_vgbp", "max_trade_vchf", "max_trade_vnxau") if hasattr(cfg, f)),
        2000.0,
    )
    return float(lo), float(hi)


def _size_grid(cfg: BotConfig) -> list[float]:
    lo, hi = _trade_bounds(cfg)
    step = float(cfg.sizing_coarse_step or 100) or 100.0
    n = max(1, int(cfg.max_sizing_quotes or 1))
    sizes: list[float] = []
    s = lo
    for _ in range(n):
        if s > hi:
            break
        sizes.append(round(s, 6))
        s += step
    return sizes or [lo]


def _is_winner(sim: LoopSimulation, cfg: BotConfig) -> bool:
    return sim.profitable and sim.floors_ok and sim.net_profit_usd >= cfg.min_profit_usd


async def select_best_loop(
    client: httpx.AsyncClient,
    chains: dict[str, ChainConfig],
    token: TokenConfig,
    cfg: BotConfig,
) -> LoopSelection:
    loops = active_loops(cfg, token)
    if not loops:
        return LoopSelection(reason="no active loops")

    grid = _size_grid(cfg)
    probe = grid[len(grid) // 2]
    candidates: list[LoopSimulation] = []

    # Phase 1: best loop family at the probe size.
    best_loop = None
    best_sim: LoopSimulation | None = None
    for loop in loops:
        sim = await simulate_loop(client, chains, token, cfg, loop, probe)
        candidates.append(sim)
        if _is_winner(sim, cfg) and (best_sim is None or sim.net_profit_usd > best_sim.net_profit_usd):
            best_loop, best_sim = loop, sim

    if best_loop is None or best_sim is None:
        return LoopSelection(
            reason=f"no profitable loop at size {probe:g} (min ${cfg.min_profit_usd:.2f})",
            candidates=candidates,
        )

    # Phase 2: size-search the winning loop for the best size.
    best = best_sim
    for size in grid:
        if size == probe:
            continue
        sim = await simulate_loop(client, chains, token, cfg, best_loop, size)
        candidates.append(sim)
        if _is_winner(sim, cfg) and sim.net_profit_usd > best.net_profit_usd:
            best = sim

    return LoopSelection(
        best=best,
        reason=f"best={best.loop_key} size={best.size:g} profit=${best.net_profit_usd:.2f}",
        candidates=candidates,
    )
