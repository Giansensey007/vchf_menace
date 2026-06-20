"""Hardcoded VNX platform deposit truth — not overridable via env."""

from __future__ import annotations

# VNX platform ETH mainnet accepts USDC only — never USDT
VNX_ETH_DEPOSIT_ASSET = "USDC"

# Per-chain hub stables (see config/chains.yaml + config/tokens.yaml)
CELO_HUB_STABLE = "USDT"
ETH_HUB_STABLE = "USDC"

VNX_ETH_BLOCKCHAIN_CODES = frozenset({"ETH", "ETHEREUM"})


def normalize_blockchain(blockchain: str) -> str:
    return blockchain.strip().upper()


def is_eth_blockchain(blockchain: str) -> bool:
    return normalize_blockchain(blockchain) in VNX_ETH_BLOCKCHAIN_CODES


def check_vnx_eth_deposit_asset(asset: str, blockchain: str | None = None) -> str | None:
    """
    Reject non-USDC assets on ETH→VNX deposit paths.

    Celo/Sol hub stables are not validated here — only Ethereum mainnet VNX credits.
    """
    if blockchain is not None and not is_eth_blockchain(blockchain):
        return None
    asset_u = asset.strip().upper()
    if asset_u == VNX_ETH_DEPOSIT_ASSET:
        return None
    return (
        f"VNX Ethereum deposits accept {VNX_ETH_DEPOSIT_ASSET} only — "
        f"refusing {asset_u} on ETH "
        f"(Celo hub uses {CELO_HUB_STABLE}; swap USDT→USDC on ETH before eth_to_vnx)"
    )


def check_eth_hub_stable_for_vnx(hub_stable: str, *, context: str = "") -> str | None:
    """Validate ethereum.hub_stable in chains.yaml matches VNX deposit asset."""
    stable_u = hub_stable.strip().upper()
    if stable_u == ETH_HUB_STABLE:
        return None
    prefix = f"{context}: " if context else ""
    return (
        f"{prefix}Ethereum hub_stable must be {ETH_HUB_STABLE} for VNX deposits "
        f"(got {stable_u}; Celo hub is {CELO_HUB_STABLE} — do not confuse chains)"
    )
