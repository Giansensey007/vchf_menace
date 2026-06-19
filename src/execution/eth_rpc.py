from __future__ import annotations

import logging
import os

from web3 import Web3

from src.config_loader import ETH_RPC_FALLBACKS

logger = logging.getLogger(__name__)


def connect_eth_web3(preferred: str | None = None) -> Web3:
    """Connect to Ethereum mainnet, trying fallbacks if the preferred RPC fails."""
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred.rstrip("/"))
    env = os.getenv("RPC_ETHEREUM", "").strip()
    if env and env not in candidates:
        candidates.append(env.rstrip("/"))
    for url in ETH_RPC_FALLBACKS:
        if url not in candidates:
            candidates.append(url)

    last_err: Exception | None = None
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 45}))
            if w3.is_connected():
                w3.eth.block_number  # verify RPC responds
                if preferred and url != preferred.rstrip("/"):
                    logger.warning("Using ETH RPC fallback: %s", url)
                return w3
        except Exception as exc:
            last_err = exc
            logger.debug("ETH RPC %s failed: %s", url, exc)
    raise ConnectionError(f"Ethereum RPC unreachable (tried {len(candidates)} endpoints): {last_err}")
