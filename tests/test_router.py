from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import ChainConfig
from src.quotes.router import quote_best
from src.quotes.types import ProviderQuote


@pytest.fixture
def celo_chain():
    return ChainConfig(
        key="base",
        name="Celo",
        chain_id=42220,
        enabled=True,
        bridge_verified=True,
        quote_tier="onchain",
        hub_stable="USDT",
        hub_token="0x48065fbbe25f71c9282ddf5e1cd6d6a887483d5e",
        hub_decimals=6,
        rpc_env="RPC_BASE",
        quoter_v2="0x82825d0554fA07f7FC52Ab63c961F330fdEFa8E8",
    )


@pytest.mark.asyncio
async def test_quote_best_onchain(celo_chain):
    amount = 1_000_000
    fake = [ProviderQuote("uniswap_v3", amount, 900_000)]

    with patch("src.quotes.router.onchain.quote_onchain_pools", return_value=fake):
        result = await quote_best(
            AsyncMock(),
            celo_chain,
            "0xc5ebea9984c485ec5d58ca5a2d376620d93af871",
            celo_chain.hub_token,
            amount,
            18,
            6,
            "VCHF",
        )
    assert result is not None
    assert result.provider == "uniswap_v3"


@pytest.mark.asyncio
async def test_quote_best_jupiter(celo_chain):
    sol = ChainConfig(
        key="solana",
        name="Solana",
        chain_id=0,
        enabled=True,
        bridge_verified=True,
        quote_tier="jupiter",
        hub_stable="USDC",
        hub_token="mint_usdc",
        hub_decimals=6,
        rpc_env="RPC_SOLANA",
    )
    amount = 1_000_000
    fake = ProviderQuote("jupiter", amount, 1_350_000)

    with patch("src.quotes.router.jupiter.quote", new=AsyncMock(return_value=fake)):
        result = await quote_best(
            AsyncMock(), sol, "mint_vchf", sol.hub_token, amount, 9, 6, "VCHF"
        )
    assert result is not None
    assert result.provider == "jupiter"
    assert result.amount_out == 1_350_000


@pytest.mark.asyncio
async def test_quote_best_vnx_requires_symbol(celo_chain):
    vnx = ChainConfig(
        key="vnx",
        name="VNX",
        chain_id=0,
        enabled=True,
        bridge_verified=True,
        quote_tier="vnx",
        hub_stable="USDC",
        hub_token="USDC",
        hub_decimals=6,
        rpc_env="VNX_API_PUBLIC_KEY",
    )
    result = await quote_best(AsyncMock(), vnx, "USDC", "VCHF", 1_000_000, 6, 18, "")
    assert result is None
