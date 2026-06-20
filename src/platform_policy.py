"""Platform-only treasury: VCHF on VNX; chains hold hub stables only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config_loader import BotConfig

if TYPE_CHECKING:
    from src.scanner.routes import RouteSpec

TOKEN_SYMBOL = "VCHF"


def platform_token_only(cfg: BotConfig) -> bool:
    return cfg.platform_vchf_only and cfg.treasury_vchf_home == "platform"


def on_chain_token_buy_blocked(cfg: BotConfig, chain_key: str) -> bool:
    """Block acquiring VCHF on-chain with stables when treasury is platform-only."""
    return platform_token_only(cfg) and chain_key != "vnx"


def on_chain_buy_blocked_message(cfg: BotConfig, chain_key: str) -> str:
    return (
        f"on-chain {TOKEN_SYMBOL} buy on {chain_key} blocked "
        f"(platform_vchf_only; withdraw from VNX platform or use vnx_to_* routes)"
    )


def route_requires_on_chain_token_buy(route: RouteSpec) -> bool:
    return route.buy_chain != "vnx"
