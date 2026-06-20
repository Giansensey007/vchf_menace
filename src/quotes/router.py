from __future__ import annotations

import asyncio

import httpx

from src.config_loader import ChainConfig, TokenConfig, load_bot_config, token_decimals
from src.platform_policy import on_chain_token_buy_blocked
from src.quotes import jupiter, kyber, onchain, vnx
from src.quotes.api_gate import api_sync
from src.quotes.types import ProviderQuote, QuoteResult


async def _evm_quotes(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
    token_symbol: str,
) -> list[ProviderQuote]:
    providers: list[ProviderQuote] = []
    kyber_q = await kyber.quote(client, chain, token_in, token_out, amount_in)
    providers.append(kyber_q)
    if not kyber_q.ok:
        pool_quotes = await asyncio.to_thread(
            onchain.quote_onchain_pools, chain, token_in, token_out, amount_in, token_symbol
        )
        providers.extend(pool_quotes)
    return providers


async def quote_best(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
    src_decimals: int,
    dest_decimals: int,
    token_symbol: str = "",
) -> QuoteResult | None:
    if amount_in <= 0:
        return None

    if chain.quote_tier == "aggregator":
        await api_sync("kyber")
        providers = await _evm_quotes(client, chain, token_in, token_out, amount_in, token_symbol)
    elif chain.quote_tier == "onchain":
        await api_sync("base_rpc")
        providers = await asyncio.to_thread(
            onchain.quote_onchain_pools, chain, token_in, token_out, amount_in, token_symbol
        )
    elif chain.quote_tier == "jupiter":
        providers = [await jupiter.quote(client, token_in, token_out, amount_in)]
    elif chain.quote_tier == "vnx":
        if not token_symbol:
            return None
        if token_in == chain.hub_token:
            pq = await vnx.quote_buy_token_with_usdc(
                client, token_symbol, amount_in, dest_decimals, src_decimals
            )
        else:
            pq = await vnx.quote_sell_token_for_usdc(
                client, token_symbol, amount_in, src_decimals, dest_decimals
            )
        providers = [pq]
    else:
        return None

    valid = [p for p in providers if p.ok]
    if not valid:
        return None
    best = max(valid, key=lambda p: p.amount_out)
    return QuoteResult(
        provider=best.provider,
        amount_in=amount_in,
        amount_out=best.amount_out,
        route_dexs=best.route_dexs,
        all_providers=providers,
        token_in=token_in,
        token_out=token_out,
        chain_key=chain.key,
        hub_stable=chain.hub_stable,
    )


async def sell_token_for_stable(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    token: TokenConfig,
    chain_key: str,
    amount_in: int,
) -> QuoteResult | None:
    token_addr = token.chains.get(chain_key)
    if not token_addr:
        return None
    dec = token_decimals(token, chain_key)
    return await quote_best(
        client, chain, token_addr, chain.hub_token, amount_in, dec, chain.hub_decimals, token.symbol
    )


async def buy_token_with_stable(
    client: httpx.AsyncClient,
    chain: ChainConfig,
    token: TokenConfig,
    chain_key: str,
    stable_amount: int,
    *,
    is_buyback: bool = False,
) -> QuoteResult | None:
    # On-chain buys are blocked under platform-only EXCEPT a loop-closing buy-back
    # (re-acquiring the same token to complete a same-asset round trip).
    if on_chain_token_buy_blocked(load_bot_config(), chain_key, is_buyback=is_buyback):
        return None
    token_addr = token.chains.get(chain_key)
    if not token_addr:
        return None
    dec = token_decimals(token, chain_key)
    return await quote_best(
        client, chain, chain.hub_token, token_addr, stable_amount, chain.hub_decimals, dec, token.symbol
    )
