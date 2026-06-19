from __future__ import annotations

from web3 import Web3


def normalize_address(addr: str, chain_type: str = "evm") -> str:
    if not addr or not isinstance(addr, str):
        return addr
    addr = addr.strip()
    if chain_type == "vnx":
        return addr
    if chain_type == "solana":
        if len(addr) < 32 or len(addr) > 44:
            raise ValueError(f"Invalid Solana mint length: {addr}")
        return addr
    return checksum(addr)


def checksum(addr: str) -> str:
    if not addr or not isinstance(addr, str):
        return addr
    addr = addr.strip()
    if not addr.startswith("0x"):
        return addr
    hex_part = addr[2:]
    if len(hex_part) != 40:
        raise ValueError(f"Invalid address length ({len(hex_part)} hex chars): {addr}")
    return Web3.to_checksum_address(hex_part.lower())
