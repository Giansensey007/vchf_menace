"""Platform-only treasury: VCHF on VNX; chains hold hub stables only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config_loader import BotConfig

if TYPE_CHECKING:
    from src.scanner.routes import RouteSpec

TOKEN_SYMBOL = "VCHF"


def platform_token_only(cfg: BotConfig) -> bool:
    return cfg.platform_vchf_only and cfg.treasury_vchf_home == "platform"


def on_chain_token_buy_blocked(
    cfg: BotConfig, chain_key: str, *, is_buyback: bool = False
) -> bool:
    """Block acquiring VCHF on-chain with stables when treasury is platform-only.

    Exception: a loop-closing **buy-back** re-acquires the SAME token to complete a
    same-asset round trip (Loop 2 / Loop 3 step 4). That is permitted even under
    platform-only because it does not open new inventory. An inventory-opening buy is
    still blocked.
    """
    if chain_key == "vnx":
        return False
    if not platform_token_only(cfg):
        return False
    return not is_buyback


def platform_buy_opener_blocked(cfg: BotConfig, *, is_buyback: bool = False) -> bool:
    """Block a platform buy that OPENS inventory.

    Under platform-only the bot never buys the token on the platform to open a cycle;
    you supply it. The only permitted platform buy is the Loop 1 step-5 **buy-back**
    that closes a same-asset round trip (pass ``is_buyback=True``).
    """
    return platform_token_only(cfg) and not is_buyback


def on_chain_buy_blocked_message(cfg: BotConfig, chain_key: str) -> str:
    return (
        f"on-chain {TOKEN_SYMBOL} buy on {chain_key} blocked "
        f"(platform_vchf_only; only a loop-closing buy-back may re-acquire {TOKEN_SYMBOL})"
    )


def platform_buy_opener_blocked_message(cfg: BotConfig) -> str:
    return (
        f"platform {TOKEN_SYMBOL} buy blocked (platform_vchf_only; bot never opens "
        f"inventory — supply {TOKEN_SYMBOL} on VNX; only Loop 1 buy-back may buy)"
    )


def route_requires_on_chain_token_buy(route: RouteSpec) -> bool:
    return route.buy_chain != "vnx"
