from __future__ import annotations

from dataclasses import dataclass

from src.config_loader import BotConfig, load_bot_config

# Dual EVM hubs: Celo (USDT) + Base (USDC) alongside Sol/VNX — 10 directed routes.
ROUTE_PAIRS: tuple[tuple[str, str], ...] = (
    ("celo", "solana"),
    ("solana", "celo"),
    ("celo", "vnx"),
    ("vnx", "celo"),
    ("base", "solana"),
    ("solana", "base"),
    ("base", "vnx"),
    ("vnx", "base"),
    ("solana", "vnx"),
    ("vnx", "solana"),
)

CELO_SOL_DIRECTIONS: tuple[str, ...] = ("celo_to_solana", "solana_to_celo")
BASE_SOL_DIRECTIONS: tuple[str, ...] = ("base_to_solana", "solana_to_base")
VNX_SOL_DIRECTIONS: tuple[str, ...] = ("solana_to_vnx", "vnx_to_solana")
CELO_VNX_DIRECTIONS: tuple[str, ...] = ("celo_to_vnx", "vnx_to_celo")
BASE_VNX_DIRECTIONS: tuple[str, ...] = ("base_to_vnx", "vnx_to_base")
EVM_VNX_DIRECTIONS: tuple[str, ...] = CELO_VNX_DIRECTIONS + BASE_VNX_DIRECTIONS


@dataclass(frozen=True)
class RouteSpec:
    buy_chain: str
    sell_chain: str

    @property
    def direction(self) -> str:
        return f"{self.buy_chain}_to_{self.sell_chain}"

    @property
    def route_group(self) -> str:
        if self.direction in CELO_SOL_DIRECTIONS:
            return "celo_sol"
        if self.direction in BASE_SOL_DIRECTIONS:
            return "base_sol"
        if self.direction in VNX_SOL_DIRECTIONS:
            return "vnx_sol"
        if self.direction in CELO_VNX_DIRECTIONS:
            return "celo_vnx"
        return "base_vnx"

    @property
    def needs_vchf_bridge(self) -> bool:
        chains = {self.buy_chain, self.sell_chain}
        return chains in ({"celo", "solana"}, {"base", "solana"}) or "vnx" in chains

    @property
    def needs_stable_bridge(self) -> bool:
        return {self.buy_chain, self.sell_chain} in ({"celo", "solana"}, {"base", "solana"})

    @property
    def needs_cctp(self) -> bool:
        return self.direction in VNX_SOL_DIRECTIONS

    @property
    def needs_bridge(self) -> bool:
        return self.needs_vchf_bridge or self.needs_stable_bridge or self.needs_cctp

    @property
    def needs_vnx_usdc(self) -> bool:
        return self.direction in EVM_VNX_DIRECTIONS

    @property
    def bridge_from(self) -> str | None:
        if not self.needs_bridge:
            return None
        if self.buy_chain == "vnx":
            return None
        if self.sell_chain == "vnx":
            return self.buy_chain
        return self.buy_chain

    @property
    def bridge_to(self) -> str | None:
        if not self.needs_bridge:
            return None
        if self.buy_chain == "vnx":
            return self.sell_chain
        if self.sell_chain == "vnx":
            return None
        if {self.buy_chain, self.sell_chain} in ({"celo", "solana"}, {"base", "solana"}):
            return self.sell_chain
        return self.sell_chain


ALL_ROUTES: tuple[RouteSpec, ...] = tuple(
    RouteSpec(buy, sell) for buy, sell in ROUTE_PAIRS
)

ALL_DIRECTIONS: tuple[str, ...] = tuple(r.direction for r in ALL_ROUTES)

CORE_ARB_DIRECTIONS: tuple[str, ...] = (
    *CELO_SOL_DIRECTIONS,
    *BASE_SOL_DIRECTIONS,
    *CELO_VNX_DIRECTIONS,
    *BASE_VNX_DIRECTIONS,
    *VNX_SOL_DIRECTIONS,
)


def active_routes(cfg: BotConfig | None = None) -> tuple[RouteSpec, ...]:
    cfg = cfg or load_bot_config()
    routes: list[RouteSpec] = []
    for r in ALL_ROUTES:
        if r.route_group == "celo_sol":
            routes.append(r)
        elif r.route_group == "base_sol":
            routes.append(r)
        elif r.route_group == "vnx_sol" and cfg.enable_vnx_cctp_routes:
            routes.append(r)
        elif r.route_group in ("celo_vnx", "base_vnx") and cfg.enable_vnx_arb_routes:
            routes.append(r)
    return tuple(routes)


def active_directions(cfg: BotConfig | None = None) -> tuple[str, ...]:
    return tuple(r.direction for r in active_routes(cfg))


def route_for_direction(direction: str) -> RouteSpec | None:
    for r in ALL_ROUTES:
        if r.direction == direction:
            return r
    return None


def estimate_fees_usd(buy_chain: str, sell_chain: str, cfg: BotConfig) -> float:
    fees = 0.0
    direction = f"{buy_chain}_to_{sell_chain}"
    if buy_chain == "celo" or sell_chain == "celo":
        fees += cfg.celo_gas_usd_estimate * 2
    if buy_chain == "base" or sell_chain == "base":
        fees += cfg.base_gas_usd_estimate * 2
    if buy_chain == "ethereum" or sell_chain == "ethereum":
        fees += cfg.eth_gas_usd_estimate * 2
    if buy_chain == "solana" or sell_chain == "solana":
        fees += cfg.solana_fee_usd_estimate
    chains = {buy_chain, sell_chain}
    if chains == {"celo", "solana"}:
        fees += cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd
    elif chains == {"base", "solana"}:
        fees += cfg.vnx_bridge_fee_usd + cfg.wormhole_bridge_fee_usd
    elif direction in VNX_SOL_DIRECTIONS:
        fees += cfg.vnx_platform_fee_usd + cfg.vnx_bridge_fee_usd
    elif "vnx" in chains:
        fees += cfg.vnx_platform_fee_usd
        if buy_chain != "vnx" and sell_chain != "vnx":
            fees += cfg.vnx_bridge_fee_usd
    return fees


def estimate_cctp_usdc_return_fees(cfg: BotConfig) -> float:
    return cfg.cctp_fee_usd + cfg.eth_gas_usd_estimate + cfg.vnx_platform_fee_usd


CCTP_SOL_USDC_TO_VNX = "cctp_sol_usdc_to_vnx"
