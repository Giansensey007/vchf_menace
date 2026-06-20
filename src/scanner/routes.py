from __future__ import annotations

from dataclasses import dataclass

from src.config_loader import BotConfig, load_bot_config

# Directed arb routes: buy VCHF on `buy`, sell VCHF on `sell`
ROUTE_PAIRS: tuple[tuple[str, str], ...] = (
    ("base", "solana"),
    ("solana", "base"),
    ("base", "vnx"),
    ("vnx", "base"),
    ("solana", "vnx"),
    ("vnx", "solana"),
)

BASE_SOL_DIRECTIONS: tuple[str, ...] = ("base_to_solana", "solana_to_base")
VNX_SOL_DIRECTIONS: tuple[str, ...] = ("solana_to_vnx", "vnx_to_solana")
BASE_VNX_DIRECTIONS: tuple[str, ...] = ("base_to_vnx", "vnx_to_base")


@dataclass(frozen=True)
class RouteSpec:
    buy_chain: str
    sell_chain: str

    @property
    def direction(self) -> str:
        return f"{self.buy_chain}_to_{self.sell_chain}"

    @property
    def route_group(self) -> str:
        if self.direction in BASE_SOL_DIRECTIONS:
            return "base_sol"
        if self.direction in VNX_SOL_DIRECTIONS:
            return "vnx_sol"
        return "base_vnx"

    @property
    def needs_vchf_bridge(self) -> bool:
        chains = {self.buy_chain, self.sell_chain}
        return chains == {"base", "solana"} or "vnx" in chains

    @property
    def needs_stable_bridge(self) -> bool:
        return {self.buy_chain, self.sell_chain} == {"base", "solana"}

    @property
    def needs_cctp(self) -> bool:
        """USDC settlement Sol ↔ ETH ↔ VNX platform via Circle CCTP."""
        return self.direction in VNX_SOL_DIRECTIONS

    @property
    def needs_bridge(self) -> bool:
        return self.needs_vchf_bridge or self.needs_stable_bridge or self.needs_cctp

    @property
    def needs_vnx_usdc(self) -> bool:
        """Base↔vnx VCHF routes; USDC path is hub_eth.base_usdc_to_vnx_usdc (Wormhole+swap)."""
        return self.direction in BASE_VNX_DIRECTIONS

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
        if {self.buy_chain, self.sell_chain} == {"base", "solana"}:
            return self.sell_chain
        return self.sell_chain


ALL_ROUTES: tuple[RouteSpec, ...] = tuple(
    RouteSpec(buy, sell) for buy, sell in ROUTE_PAIRS
)

ALL_DIRECTIONS: tuple[str, ...] = tuple(r.direction for r in ALL_ROUTES)

# Six directed arb legs on Base / Sol / VNX (excludes ETH hub settlement pair).
CORE_ARB_DIRECTIONS: tuple[str, ...] = (
    *BASE_SOL_DIRECTIONS,
    *BASE_VNX_DIRECTIONS,
    *VNX_SOL_DIRECTIONS,
)


def active_routes(cfg: BotConfig | None = None) -> tuple[RouteSpec, ...]:
    cfg = cfg or load_bot_config()
    routes: list[RouteSpec] = []
    for r in ALL_ROUTES:
        if r.route_group == "base_sol":
            routes.append(r)
        elif r.route_group == "vnx_sol" and cfg.enable_vnx_cctp_routes:
            routes.append(r)
        elif r.route_group == "base_vnx" and cfg.enable_vnx_arb_routes:
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
    """Per-leg execution fees (CCTP is only on the USDC return path, not each VNX↔Sol leg)."""
    fees = 0.0
    direction = f"{buy_chain}_to_{sell_chain}"
    if buy_chain == "base" or sell_chain == "base":
        fees += cfg.base_gas_usd_estimate * 2
    if buy_chain == "ethereum" or sell_chain == "ethereum":
        fees += cfg.eth_gas_usd_estimate * 2
    if buy_chain == "solana" or sell_chain == "solana":
        fees += cfg.solana_fee_usd_estimate
    chains = {buy_chain, sell_chain}
    if chains == {"base", "solana"}:
        fees += cfg.vnx_bridge_fee_usd
        fees += cfg.wormhole_bridge_fee_usd
    elif direction in VNX_SOL_DIRECTIONS:
        fees += cfg.vnx_platform_fee_usd
        fees += cfg.vnx_bridge_fee_usd
    elif "vnx" in chains:
        fees += cfg.vnx_platform_fee_usd
        if buy_chain != "vnx" and sell_chain != "vnx":
            fees += cfg.vnx_bridge_fee_usd
    return fees


def estimate_cctp_usdc_return_fees(cfg: BotConfig) -> float:
    """Sol USDC → CCTP → ETH → VNX USDC deposit → platform VCHF buy."""
    return cfg.cctp_fee_usd + cfg.eth_gas_usd_estimate + cfg.vnx_platform_fee_usd


# Synthetic closed-loop return (not a directed buy/sell route pair)
CCTP_SOL_USDC_TO_VNX = "cctp_sol_usdc_to_vnx"
