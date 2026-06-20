from __future__ import annotations

import logging

import httpx

from src.config_loader import ChainConfig, load_bridge_config
from src.quotes import jupiter
from src.quotes.types import from_human, to_human

logger = logging.getLogger(__name__)


def stable_amount_usdt(
    chain_key: str,
    chain: ChainConfig,
    amount_raw: int,
) -> float:
    """Return human USDT-equivalent for a hub stable amount on `chain`."""
    human = float(to_human(amount_raw, chain.hub_decimals))
    if chain_key == "base":
        return human  # native USDT
    if chain.hub_stable == "USDT":
        return human
    return human  # USDC treated as ~1 until async conversion


async def usdc_to_usdt_solana(
    client: httpx.AsyncClient,
    usdc_amount_raw: int,
    bridge_cfg: dict | None = None,
) -> tuple[float, int | None]:
    """Quote Solana USDC → USDT via Jupiter; returns (usdt_human, usdt_raw)."""
    cfg = bridge_cfg or load_bridge_config()
    wh = cfg["wormhole"]
    usdc_mint = wh["solana_usdc"]
    usdt_mint = wh["solana_usdt"]
    if usdc_amount_raw <= 0:
        return 0.0, 0

    q = await jupiter.quote(client, usdc_mint, usdt_mint, usdc_amount_raw)
    if not q.ok:
        # Fallback: 1:1 with warning
        human = float(to_human(usdc_amount_raw, 6))
        logger.warning("USDC→USDT Jupiter quote failed, using 1:1: %s", q.error)
        return human, usdc_amount_raw

    usdt_human = float(to_human(q.amount_out, 6))
    return usdt_human, q.amount_out


async def usdt_to_usdc_solana(
    client: httpx.AsyncClient,
    usdt_amount_raw: int,
    bridge_cfg: dict | None = None,
) -> tuple[int | None, str | None]:
    """Quote USDT → USDC on Solana for DEX legs; returns (usdc_raw, error)."""
    cfg = bridge_cfg or load_bridge_config()
    wh = cfg["wormhole"]
    q = await jupiter.quote(client, wh["solana_usdt"], wh["solana_usdc"], usdt_amount_raw)
    if not q.ok:
        return None, q.error
    return q.amount_out, None


async def normalize_hub_to_usdt(
    client: httpx.AsyncClient,
    chain_key: str,
    chain: ChainConfig,
    amount_raw: int,
) -> float:
    """Convert any hub stable leg to USDT for cross-chain PnL."""
    if chain_key == "base":
        return float(to_human(amount_raw, chain.hub_decimals))
    if chain.hub_stable == "USDT":
        return float(to_human(amount_raw, chain.hub_decimals))
    if chain_key == "solana" and chain.hub_stable == "USDC":
        usdt_human, _ = await usdc_to_usdt_solana(client, amount_raw)
        return usdt_human
    return float(to_human(amount_raw, chain.hub_decimals))


def usdt_raw_for_base_buy(usdt_human: float) -> int:
    return from_human(usdt_human, 6)


async def usdc_raw_for_solana_buy(
    client: httpx.AsyncClient,
    usdt_human: float,
) -> tuple[int | None, str | None]:
    """Given USDT budget, get USDC amount to spend on Solana Jupiter leg."""
    usdt_raw = from_human(usdt_human, 6)
    usdc_raw, err = await usdt_to_usdc_solana(client, usdt_raw)
    if err:
        return from_human(usdt_human, 6), None  # fallback 1:1
    return usdc_raw, None
