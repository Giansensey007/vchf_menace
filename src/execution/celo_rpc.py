"""Celo Web3 connection with RPC fallbacks."""

from __future__ import annotations

import logging
import os

from web3 import Web3

from src.config_loader import DEFAULT_RPC

logger = logging.getLogger(__name__)

CELO_RPC_FALLBACKS: tuple[str, ...] = (
    "https://forno.celo.org",
    "https://celo-mainnet.public.blastapi.io",
    "https://rpc.ankr.com/celo",
    "https://1rpc.io/celo",
)


def connect_celo_web3(preferred: str | None = None, *, timeout: int = 30) -> Web3:
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred.rstrip("/"))
    env = os.getenv("RPC_CELO", "").strip()
    if env and env not in candidates:
        candidates.append(env.rstrip("/"))
    default = DEFAULT_RPC.get("RPC_CELO", "")
    if default and default not in candidates:
        candidates.append(default)
    for url in CELO_RPC_FALLBACKS:
        if url not in candidates:
            candidates.append(url)

    last_err: Exception | None = None
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
            if w3.is_connected():
                w3.eth.block_number
                if preferred and url != preferred.rstrip("/"):
                    logger.warning("Using Celo RPC fallback: %s", url)
                return w3
        except Exception as exc:
            last_err = exc
            logger.debug("Celo RPC %s failed: %s", url, exc)
    raise ConnectionError(f"Celo RPC unreachable (tried {len(candidates)} endpoints): {last_err}")
