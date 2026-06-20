from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from src.config_loader import BotConfig, load_bot_config, load_chains, load_tokens
from src.quotes.http_client import build_client
from src.scanner.routes import active_directions
from src.scanner.selector import SelectionResult, select_execution_route
from src.scanner.simulator import CycleSimulation, simulate_direction
from src.scanner.sizing import cap_sizes, search_profitable_size

logger = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    direction: str
    buy_chain: str
    sell_chain: str
    size_vchf: float
    net_profit_usd: float
    simulation: CycleSimulation
    scanned_at: float
    route_group: str = ""
    selection_reason: str = ""
    celo_sol_net: float | None = None
    base_sol_net: float | None = None
    vnx_sol_net: float | None = None


@dataclass
class ScanSummary:
    selection: SelectionResult
    all_opportunities: list[ArbOpportunity] = field(default_factory=list)


class ArbScanner:
    def __init__(self, bot_cfg: BotConfig | None = None) -> None:
        self.chains = load_chains()
        self.tokens = load_tokens()
        self.token = self.tokens["VCHF"]
        self.bot_cfg = bot_cfg or load_bot_config()
        self.last_scan_at: float | None = None
        self.last_selection: SelectionResult | None = None

    async def scan_deploy(self, client: httpx.AsyncClient | None = None) -> ScanSummary:
        """
        Deploy scan: parallel celo↔sol + SOL↔platform groups, then selection rules.
        """
        own = client is None
        if own:
            client = build_client()
        try:
            selection = await select_execution_route(client, self.chains, self.token, self.bot_cfg)
            self.last_selection = selection
            opps: list[ArbOpportunity] = []
            for best in (
                selection.celo_sol,
                selection.base_sol,
                selection.vnx_sol,
                selection.celo_vnx,
                selection.base_vnx,
            ):
                if best is None:
                    continue
                opps.append(
                    ArbOpportunity(
                        direction=best.direction,
                        buy_chain=best.simulation.buy_chain,
                        sell_chain=best.simulation.sell_chain,
                        size_vchf=best.size_vchf,
                        net_profit_usd=best.net_profit_usd,
                        simulation=best.simulation,
                        scanned_at=time.time(),
                        route_group=best.group,
                        selection_reason=selection.reason,
                        celo_sol_net=selection.celo_sol.net_profit_usd if selection.celo_sol else None,
                        base_sol_net=selection.base_sol.net_profit_usd if selection.base_sol else None,
                        vnx_sol_net=selection.vnx_sol.net_profit_usd if selection.vnx_sol else None,
                    )
                )
            self.last_scan_at = time.time()
            return ScanSummary(selection, opps)
        finally:
            if own:
                await client.aclose()

    async def scan(self, client: httpx.AsyncClient | None = None) -> list[ArbOpportunity]:
        summary = await self.scan_deploy(client)
        if summary.selection.opportunity is None:
            return []
        best = summary.selection.opportunity
        return [
            ArbOpportunity(
                direction=best.direction,
                buy_chain=best.simulation.buy_chain,
                sell_chain=best.simulation.sell_chain,
                size_vchf=best.size_vchf,
                net_profit_usd=best.net_profit_usd,
                simulation=best.simulation,
                scanned_at=time.time(),
                route_group=best.group,
                selection_reason=summary.selection.reason,
                celo_sol_net=summary.selection.celo_sol.net_profit_usd
                if summary.selection.celo_sol
                else None,
                base_sol_net=summary.selection.base_sol.net_profit_usd
                if summary.selection.base_sol
                else None,
                vnx_sol_net=summary.selection.vnx_sol.net_profit_usd if summary.selection.vnx_sol else None,
            )
        ]

    async def best_opportunity(self) -> ArbOpportunity | None:
        opps = await self.scan()
        return opps[0] if opps else None

    async def scan_matrix(
        self, client: httpx.AsyncClient | None = None, *, use_probe_grid: bool = False
    ) -> dict[str, list[CycleSimulation]]:
        own = client is None
        if own:
            client = build_client()
        matrix: dict[str, list[CycleSimulation]] = {}
        try:
            for direction in active_directions(self.bot_cfg):
                matrix[direction] = []
                if use_probe_grid:
                    sizes = cap_sizes(self.bot_cfg.probe_sizes, self.bot_cfg.max_trade_vchf)
                else:
                    sizes = [self.bot_cfg.min_trade_vchf, self.bot_cfg.max_trade_vchf]
                for size in sizes:
                    sim = await simulate_direction(
                        client, self.chains, self.token, self.bot_cfg, direction, size
                    )
                    matrix[direction].append(sim)
        finally:
            if own:
                await client.aclose()
        return matrix
