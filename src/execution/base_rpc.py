"""Base Web3 connection with RPC fallbacks."""

from __future__ import annotations

import logging
import os

from web3 import Web3

from src.config_loader import DEFAULT_RPC

logger = logging.getLogger(__name__)

BASE_RPC_FALLBACKS: tuple[str, ...] = (
    "https://base.llamarpc.com",
    "https://base-mainnet.public.blastapi.io",
    "https://rpc.ankr.com/base",
    "https://1rpc.io/base",
    "https://mainnet.base.org",
)


def connect_base_web3(preferred: str | None = None, *, timeout: int = 30) -> Web3:
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred.rstrip("/"))
    env = os.getenv("RPC_BASE", "").strip()
    if env and env not in candidates:
        candidates.append(env.rstrip("/"))
    default = DEFAULT_RPC.get("RPC_BASE", "")
    if default and default not in candidates:
        candidates.append(default)
    for url in BASE_RPC_FALLBACKS:
        if url not in candidates:
            candidates.append(url)

    last_err: Exception | None = None
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
            if w3.is_connected():
                w3.eth.block_number
                if preferred and url != preferred.rstrip("/"):
                    logger.warning("Using Base RPC fallback: %s", url)
                return w3
        except Exception as exc:
            last_err = exc
            logger.debug("Base RPC %s failed: %s", url, exc)
    raise ConnectionError(f"Base RPC unreachable (tried {len(candidates)} endpoints): {last_err}")
