"""Round-trip definitions for closed-loop arbitrage."""
from __future__ import annotations

# Inverse direction — returns capital toward the buy-side chain of the inverse leg
INVERSE_DIRECTION: dict[str, str] = {
    "base_to_vnx": "vnx_to_base",
    "vnx_to_base": "base_to_vnx",
    "solana_to_vnx": "vnx_to_solana",
    "vnx_to_solana": "solana_to_vnx",
    "base_to_solana": "solana_to_base",
    "solana_to_base": "base_to_solana",
}

# Where stablecoin lands after a successful one-way leg (chain_key, stable name)
LEG_END_STABLE: dict[str, tuple[str, str]] = {
    "base_to_vnx": ("vnx", "usdc"),
    "vnx_to_base": ("base", "usdt"),
    "solana_to_vnx": ("vnx", "usdc"),
    "vnx_to_solana": ("solana", "usdc"),
    "base_to_solana": ("solana", "usdc"),
    "solana_to_base": ("base", "usdt"),
}

# Chains that spend their hub stable on the buy leg
ORIGIN_BUY_CHAIN: dict[str, str] = {
    "base": "base",
    "solana": "solana",
    "vnx": "vnx",
}

# Profitable one-way legs that start by spending stable on `origin`
DIRECTIONS_FROM_ORIGIN: dict[str, tuple[str, ...]] = {
    "base": ("base_to_vnx", "base_to_solana"),
    "solana": ("solana_to_vnx", "solana_to_base"),
    "vnx": ("vnx_to_base", "vnx_to_solana"),
}


def inverse_direction(direction: str) -> str | None:
    return INVERSE_DIRECTION.get(direction)


def leg_end(direction: str) -> tuple[str, str] | None:
    return LEG_END_STABLE.get(direction)


def closes_to_origin(origin: str, direction: str) -> bool:
    """True if a successful leg already ends on origin's hub stable."""
    end = LEG_END_STABLE.get(direction)
    if not end:
        return False
    chain, _stable = end
    return chain == origin


def origin_for_direction(direction: str) -> str:
    """Hub stable spent on the buy leg (round-trip start chain)."""
    from src.scanner.routes import route_for_direction

    route = route_for_direction(direction)
    if route:
        return route.buy_chain
    return "base"


def return_closes_origin(origin: str, primary: str) -> bool:
    """True if executing the inverse after primary returns to origin stable."""
    inv = inverse_direction(primary)
    if not inv:
        return False
    return closes_to_origin(origin, inv)


def use_cctp_usdc_return(origin: str, primary: str, *, enable_cctp: bool = True) -> bool:
    """
    Platform-centric return: after vnx_to_solana, move Sol USDC via CCTP to VNX
    and rebuy VCHF (instead of VCHF bridge solana_to_vnx).
    """
    return enable_cctp and origin == "vnx" and primary == "vnx_to_solana"


def return_leg_direction(origin: str, primary: str, *, enable_cctp: bool = True) -> str | None:
    """Return leg id for closed-loop execution/simulation."""
    if use_cctp_usdc_return(origin, primary, enable_cctp=enable_cctp):
        from src.scanner.routes import CCTP_SOL_USDC_TO_VNX

        return CCTP_SOL_USDC_TO_VNX
    return inverse_direction(primary)


def return_closes_origin_with_cctp(origin: str, primary: str, *, enable_cctp: bool = True) -> bool:
    if use_cctp_usdc_return(origin, primary, enable_cctp=enable_cctp):
        return origin == "vnx"
    return return_closes_origin(origin, primary)
