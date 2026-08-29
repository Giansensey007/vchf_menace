from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.config_loader import BotConfig, TokenConfig, load_bot_config, load_tokens
from src.platform_policy import on_chain_token_buy_blocked

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
        if on_chain_token_buy_blocked(cfg, r.buy_chain):
            continue
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


# ---------------------------------------------------------------------------
# Platform-first loop model (same-asset round trips)
# ---------------------------------------------------------------------------
# Every loop starts and ends with the same VNX token on the platform. The bot
# never opens inventory with a platform buy or an on-chain buy; on-chain/platform
# buys are only the loop-closing buy-back step (see platform_policy).
#
#   Loop 1 (outbound): withdraw token -> sell on X -> bridge stable X->ETH ->
#                      VNX USDC deposit -> platform buy-back of same token.
#   Loop 2 (inbound):  platform sell token -> USDC -> ETH -> bridge ETH->X ->
#                      on-chain buy-back on X -> VNX token deposit.
#   Loop 3 (cross):    withdraw token -> sell on A -> direct bridge A->B ->
#                      on-chain buy-back on B -> VNX token deposit.

# Hub stable per chain — drives bridge mechanism + accounting.
CHAIN_STABLE: dict[str, str] = {
    "celo": "USDT",
    "base": "USDC",
    "solana": "USDC",
    "ethereum": "USDC",
}

# Chains whose native USDC is a Circle CCTP domain (direct burn-and-mint).
CCTP_CHAINS: frozenset[str] = frozenset({"solana", "ethereum", "base"})

# VNX USDC settlement chain (deposits/withdrawals to the platform clear here).
HUB_CHAIN = "ethereum"

LOOP1_OUTBOUND = "loop1_outbound"
LOOP2_INBOUND = "loop2_inbound"
LOOP3_CROSS = "loop3_cross"


def bridge_mechanism(from_chain: str, to_chain: str) -> str:
    """CCTP-first stable bridge selection.

    Preference order: CCTP (USDC<->USDC direct) > Wormhole (any Celo USDT leg) >
    ETH triangle (cross-stable pairs with no native direct bridge, e.g. Celo<->Base).
    """
    if from_chain == to_chain:
        return "none"
    pair = {from_chain, to_chain}
    if pair <= CCTP_CHAINS:
        return "cctp"
    if "celo" in pair and pair <= {"celo", "solana", "ethereum"}:
        return "wormhole"
    return "eth_triangle"


class StepKind(str, Enum):
    WITHDRAW_TOKEN = "withdraw_token"            # platform -> chain
    SELL_TOKEN_ONCHAIN = "sell_token_onchain"    # token -> stable on chain
    PLATFORM_SELL_TOKEN = "platform_sell_token"  # token -> USDC on platform
    WITHDRAW_USDC = "withdraw_usdc"              # platform USDC -> ETH
    BRIDGE_STABLE = "bridge_stable"              # stable from -> to (CCTP/Wormhole/triangle)
    VNX_USDC_DEPOSIT = "vnx_usdc_deposit"        # ETH USDC -> platform
    PLATFORM_BUYBACK = "platform_buyback"        # USDC -> token on platform (buy-back only)
    ONCHAIN_BUYBACK = "onchain_buyback"          # stable -> token on chain (buy-back only)
    VNX_TOKEN_DEPOSIT = "vnx_token_deposit"      # chain token -> platform


@dataclass(frozen=True)
class LoopStep:
    kind: StepKind
    venue: str
    detail: str = ""
    is_buyback: bool = False
    bridge_to: str | None = None
    mechanism: str | None = None


@dataclass(frozen=True)
class LoopSpec:
    """One full same-asset round trip (platform token -> ... -> platform token)."""

    family: str
    token: str
    chain_a: str
    chain_b: str | None = None
    hub: str = HUB_CHAIN

    @property
    def key(self) -> str:
        if self.chain_b:
            return f"{self.family}:{self.chain_a}->{self.chain_b}"
        return f"{self.family}:{self.chain_a}"

    @property
    def chains(self) -> tuple[str, ...]:
        return (self.chain_a, self.chain_b) if self.chain_b else (self.chain_a,)

    def steps(self) -> tuple[LoopStep, ...]:
        if self.family == LOOP1_OUTBOUND:
            return self._loop1_steps()
        if self.family == LOOP2_INBOUND:
            return self._loop2_steps()
        if self.family == LOOP3_CROSS:
            return self._loop3_steps()
        raise ValueError(f"unknown loop family: {self.family}")

    def _loop1_steps(self) -> tuple[LoopStep, ...]:
        x = self.chain_a
        steps = [
            LoopStep(StepKind.WITHDRAW_TOKEN, x, f"withdraw {self.token} platform->{x}"),
            LoopStep(StepKind.SELL_TOKEN_ONCHAIN, x, f"sell {self.token} for {CHAIN_STABLE[x]} on {x}"),
        ]
        # ETH-as-trading-chain (VNXAU) needs no hub bridge: USDC is already on ETH.
        if x != self.hub:
            steps.append(
                LoopStep(
                    StepKind.BRIDGE_STABLE, x, f"bridge stable {x}->{self.hub}",
                    bridge_to=self.hub, mechanism=bridge_mechanism(x, self.hub),
                )
            )
        steps.append(LoopStep(StepKind.VNX_USDC_DEPOSIT, self.hub, "deposit USDC ETH->platform"))
        steps.append(
            LoopStep(StepKind.PLATFORM_BUYBACK, "vnx", f"platform buy-back {self.token}", is_buyback=True)
        )
        return tuple(steps)

    def _loop2_steps(self) -> tuple[LoopStep, ...]:
        x = self.chain_a
        steps = [
            LoopStep(StepKind.PLATFORM_SELL_TOKEN, "vnx", f"platform sell {self.token} for USDC"),
            LoopStep(StepKind.WITHDRAW_USDC, self.hub, "withdraw USDC platform->ETH"),
        ]
        if x != self.hub:
            steps.append(
                LoopStep(
                    StepKind.BRIDGE_STABLE, self.hub, f"bridge stable {self.hub}->{x}",
                    bridge_to=x, mechanism=bridge_mechanism(self.hub, x),
                )
            )
        steps.append(
            LoopStep(
                StepKind.ONCHAIN_BUYBACK, x,
                f"buy-back {self.token} with {CHAIN_STABLE[x]} on {x}", is_buyback=True,
            )
        )
        steps.append(LoopStep(StepKind.VNX_TOKEN_DEPOSIT, x, f"deposit {self.token} {x}->platform"))
        return tuple(steps)

    def _loop3_steps(self) -> tuple[LoopStep, ...]:
        a, b = self.chain_a, self.chain_b
        assert b is not None
        return (
            LoopStep(StepKind.WITHDRAW_TOKEN, a, f"withdraw {self.token} platform->{a}"),
            LoopStep(StepKind.SELL_TOKEN_ONCHAIN, a, f"sell {self.token} for {CHAIN_STABLE[a]} on {a}"),
            LoopStep(
                StepKind.BRIDGE_STABLE, a, f"bridge stable {a}->{b}",
                bridge_to=b, mechanism=bridge_mechanism(a, b),
            ),
            LoopStep(
                StepKind.ONCHAIN_BUYBACK, b,
                f"buy-back {self.token} with {CHAIN_STABLE[b]} on {b}", is_buyback=True,
            ),
            LoopStep(StepKind.VNX_TOKEN_DEPOSIT, b, f"deposit {self.token} {b}->platform"),
        )

    @property
    def bridge_legs(self) -> tuple[LoopStep, ...]:
        return tuple(s for s in self.steps() if s.kind == StepKind.BRIDGE_STABLE)


def _bot_token(tokens: dict[str, TokenConfig]) -> TokenConfig:
    """The bot's single VNX token (the one deposited/withdrawn on the platform)."""
    for t in tokens.values():
        if "vnx" in t.chains:
            return t
    return next(iter(tokens.values()))


def trading_chains(token: TokenConfig) -> tuple[str, ...]:
    """On-chain venues where the token trades (platform 'vnx' excluded)."""
    return tuple(c for c in token.chains if c != "vnx")


def catalog_loops(token: TokenConfig | None = None) -> tuple[LoopSpec, ...]:
    """Unfiltered same-asset loops for this bot's trading chains.

    Loop 1 + Loop 2: one each per trading chain. Loop 3: every ordered distinct
    pair of trading chains. ETH is a trading chain only where the token is
    deployed on it (VNXAU); for GBP/VCHF it is a hub-only settlement chain.
    """
    if token is None:
        token = _bot_token(load_tokens())
    tcs = trading_chains(token)
    sym = token.symbol
    loops: list[LoopSpec] = []
    for x in tcs:
        loops.append(LoopSpec(LOOP1_OUTBOUND, sym, x))
        loops.append(LoopSpec(LOOP2_INBOUND, sym, x))
    for a in tcs:
        for b in tcs:
            if a != b:
                loops.append(LoopSpec(LOOP3_CROSS, sym, a, b))
    return tuple(loops)


def active_loops(
    cfg: BotConfig | None = None, token: TokenConfig | None = None
) -> tuple[LoopSpec, ...]:
    """Same-asset loops enabled by ENABLE_LOOP1 / LOOP2 / LOOP3 (default L3 only)."""
    cfg = cfg or load_bot_config()
    allowed: set[str] = set()
    if cfg.enable_loop1:
        allowed.add(LOOP1_OUTBOUND)
    if cfg.enable_loop2:
        allowed.add(LOOP2_INBOUND)
    if cfg.enable_loop3:
        allowed.add(LOOP3_CROSS)
    return tuple(loop for loop in catalog_loops(token) if loop.family in allowed)


def active_loop_keys(cfg: BotConfig | None = None) -> tuple[str, ...]:
    return tuple(loop.key for loop in active_loops(cfg))


def loop_for_key(key: str, cfg: BotConfig | None = None) -> LoopSpec | None:
    for loop in active_loops(cfg):
        if loop.key == key:
            return loop
    return None
