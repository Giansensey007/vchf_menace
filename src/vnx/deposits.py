from __future__ import annotations

import os

from src.vnx.constants import (
    CELO_HUB_STABLE,
    ETH_HUB_STABLE,
    VNX_ETH_DEPOSIT_ASSET,
    check_eth_hub_stable_for_vnx,
    check_vnx_eth_deposit_asset,
)

_DEFAULT_MIN_USDC_DEPOSIT: dict[str, float] = {"ETH": 20.0}


def min_deposit_vchf(blockchain: str) -> float:
    """Minimum cumulative on-chain VCHF deposit before VNX credits (CELO/BASE/SOL)."""
    bc = blockchain.strip().upper()
    if bc == "CELO":
        return float(os.getenv("VNX_MIN_DEPOSIT_VCHF_CELO", "5"))
    if bc == "BASE":
        return float(os.getenv("VNX_MIN_DEPOSIT_VCHF_BASE", "5"))
    if bc == "SOL":
        return float(os.getenv("VNX_MIN_DEPOSIT_VCHF_SOL", "5"))
    return 0.0


def check_deposit_amount(blockchain: str, quantity: float) -> str | None:
    """Return error message if VCHF deposit is below chain minimum, else None."""
    min_qty = min_deposit_vchf(blockchain)
    if min_qty <= 0:
        return None
    if quantity < min_qty:
        return (
            f"VNX {blockchain.upper()} deposit {quantity:.2f} VCHF below minimum "
            f"{min_qty:.2f} VCHF (cumulative on-chain transfers must reach minimum before credit)"
        )
    return None


def min_deposit_usdc(blockchain: str) -> float:
    """Minimum cumulative on-chain USDC deposit before VNX credits (ETH)."""
    bc = blockchain.strip().upper()
    if bc in ("ETH", "ETHEREUM"):
        return float(os.getenv("VNX_MIN_DEPOSIT_USDC_ETH", "20"))
    return 0.0


def check_usdc_deposit_amount(blockchain: str, quantity: float) -> str | None:
    """Return error message if USDC deposit is below chain minimum, else None."""
    min_qty = min_deposit_usdc(blockchain)
    if min_qty <= 0:
        return None
    if quantity < min_qty:
        return (
            f"VNX {blockchain.upper()} USDC deposit {quantity:.2f} below minimum "
            f"{min_qty:.2f} USDC (cumulative on-chain transfers must reach minimum before credit)"
        )
    return None


def validate_eth_usdc_vnx_deposit(
    quantity: float,
    *,
    asset: str = VNX_ETH_DEPOSIT_ASSET,
    blockchain: str = "ETH",
) -> str | None:
    """Asset + minimum guard for ETH→VNX USDC deposit (rejects USDT on ETH)."""
    asset_err = check_vnx_eth_deposit_asset(asset, blockchain)
    if asset_err:
        return asset_err
    return check_usdc_deposit_amount(blockchain, quantity)
