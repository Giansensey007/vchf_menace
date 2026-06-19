from __future__ import annotations

from web3 import Web3

from src.config_loader import ChainConfig
from src.quotes.addresses import checksum
from src.quotes.types import ProviderQuote

QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

DEFAULT_FEE_TIERS = [100, 500, 3000, 10000]


def quote_pool(
    w3: Web3,
    quoter: str,
    token_in: str,
    token_out: str,
    amount_in: int,
    fee: int,
) -> ProviderQuote:
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(quoter), abi=QUOTER_V2_ABI)
        params = (checksum(token_in), checksum(token_out), amount_in, fee, 0)
        amount_out, _, _, _ = contract.functions.quoteExactInputSingle(params).call()
        return ProviderQuote("uniswap_v3", amount_in, int(amount_out), route_dexs=[f"UniswapV3-{fee}"])
    except Exception as exc:
        return ProviderQuote("uniswap_v3", amount_in, 0, error=str(exc)[:200])


def _probe_fee_tiers(
    w3: Web3, quoter: str, token_in: str, token_out: str, amount_in: int
) -> list[ProviderQuote]:
    return [q for fee in DEFAULT_FEE_TIERS if (q := quote_pool(w3, quoter, token_in, token_out, amount_in, fee)).ok]


def quote_onchain_pools(
    chain: ChainConfig,
    token_in: str,
    token_out: str,
    amount_in: int,
    token_symbol: str,
) -> list[ProviderQuote]:
    if not chain.rpc_url or not chain.quoter_v2:
        return [ProviderQuote("uniswap_v3", amount_in, 0, error="no RPC")]
    w3 = Web3(Web3.HTTPProvider(chain.rpc_url, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        return [ProviderQuote("uniswap_v3", amount_in, 0, error="RPC unreachable")]

    pools_cfg = chain.pools or {}
    results: list[ProviderQuote] = []
    sym = token_symbol.lower()
    for pool_key, pool in pools_cfg.items():
        if sym not in pool_key:
            continue
        q = quote_pool(w3, chain.quoter_v2, token_in, token_out, amount_in, int(pool["fee"]))
        q.provider = f"uniswap_v3:{pool['address'][:10]}"
        if q.ok:
            results.append(q)

    if not results:
        results = _probe_fee_tiers(w3, chain.quoter_v2, token_in, token_out, amount_in)

    if not results:
        return [ProviderQuote("uniswap_v3", amount_in, 0, error="no on-chain pool")]
    return results
