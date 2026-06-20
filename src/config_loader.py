from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.quotes.addresses import checksum, normalize_address

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

DEFAULT_RPC: dict[str, str] = {
    "RPC_CELO": "https://forno.celo.org",
    "RPC_BASE": "https://base.llamarpc.com",
    "RPC_SOLANA": "https://api.mainnet-beta.solana.com",
    "RPC_ETHEREUM": "https://ethereum.publicnode.com",
}

SOL_RPC_FALLBACKS: tuple[str, ...] = (
    "https://api.mainnet-beta.solana.com",
    "https://api.mainnet.solana.com",
)

ETH_RPC_FALLBACKS: tuple[str, ...] = (
    "https://ethereum.publicnode.com",
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://1rpc.io/eth",
)


@dataclass
class ChainConfig:
    key: str
    name: str
    chain_id: int
    enabled: bool
    bridge_verified: bool
    quote_tier: str
    hub_stable: str
    hub_token: str
    hub_decimals: int
    rpc_env: str
    chain_type: str = "evm"
    kyber_slug: str | None = None
    quoter_v2: str | None = None
    swap_router: str | None = None
    pools: dict[str, Any] = field(default_factory=dict)

    @property
    def rpc_url(self) -> str:
        return os.getenv(self.rpc_env) or DEFAULT_RPC.get(self.rpc_env, "")


@dataclass
class TokenConfig:
    symbol: str
    decimals: int
    chains: dict[str, str]
    chain_decimals: dict[str, int] = field(default_factory=dict)


@dataclass
class BotConfig:
    poll_interval_sec: int
    min_profit_usd: float
    min_trade_vchf: float
    max_trade_vchf: float
    sizing_coarse_step: float
    max_sizing_quotes: int
    probe_sizes: list[float]
    slippage_bps: int
    quote_freshness_sec: int
    peg_min: float
    peg_max: float
    vnx_bridge_poll_sec: int
    vnx_bridge_timeout_sec: int
    celo_gas_usd_estimate: float
    base_gas_usd_estimate: float
    solana_fee_usd_estimate: float
    vnx_bridge_fee_usd: float
    vnx_platform_fee_usd: float
    wormhole_bridge_fee_usd: float
    enable_vnx_arb_routes: bool
    enable_vnx_cctp_routes: bool
    indirect_route_premium_usd: float
    eth_gas_usd_estimate: float
    cctp_fee_usd: float = 1.5
    treasury_vchf_home: str = "platform"
    vchf_on_chain_dust: float = 0.5
    platform_vchf_only: bool = True
    close_loop_after_cycle: bool = True
    close_loop_always_return: bool = True
    close_loop_min_net_usd: float = 0.0
    jit_withdraw: bool = True


def _chain_types_from_yaml() -> dict[str, str]:
    raw = yaml.safe_load((CONFIG_DIR / "chains.yaml").read_text())
    return {k: v.get("chain_type", "evm") for k, v in raw["chains"].items()}


def load_chains() -> dict[str, ChainConfig]:
    raw = yaml.safe_load((CONFIG_DIR / "chains.yaml").read_text())
    chains: dict[str, ChainConfig] = {}
    for key, cfg in raw["chains"].items():
        if not cfg.get("enabled", True):
            continue
        if not cfg.get("bridge_verified", True):
            continue
        chain_type = cfg.get("chain_type", "evm")
        fields = {k: v for k, v in cfg.items() if k not in ("key", "chain_type")}
        chain = ChainConfig(key=key, chain_type=chain_type, **fields)
        chain.hub_token = normalize_address(chain.hub_token, chain_type)
        if chain.quoter_v2:
            chain.quoter_v2 = checksum(chain.quoter_v2)
        if chain.swap_router:
            chain.swap_router = checksum(chain.swap_router)
        for pool in (chain.pools or {}).values():
            if pool.get("address"):
                pool["address"] = checksum(pool["address"])
        chains[key] = chain
    return chains


def load_tokens() -> dict[str, TokenConfig]:
    raw = yaml.safe_load((CONFIG_DIR / "tokens.yaml").read_text())
    chain_types = _chain_types_from_yaml()
    tokens: dict[str, TokenConfig] = {}
    for sym, cfg in raw["tokens"].items():
        chains_map = {
            ck: normalize_address(addr, chain_types.get(ck, "evm"))
            for ck, addr in cfg["chains"].items()
        }
        chain_decimals = {k: int(v) for k, v in (cfg.get("chain_decimals") or {}).items()}
        tokens[sym] = TokenConfig(
            symbol=sym, decimals=cfg["decimals"], chains=chains_map, chain_decimals=chain_decimals
        )
    return tokens


def load_bot_config() -> BotConfig:
    raw = yaml.safe_load((CONFIG_DIR / "bot.yaml").read_text())["bot"]
    min_trade = float(os.getenv("MIN_TRADE_VCHF", raw.get("min_trade_vchf", 200)))
    max_trade = float(os.getenv("MAX_TRADE_VCHF", raw.get("max_trade_vchf", 2000)))
    return BotConfig(
        poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", raw.get("poll_interval_sec", 60))),
        min_profit_usd=float(os.getenv("MIN_PROFIT_USD", raw.get("min_profit_usd", 5))),
        min_trade_vchf=min_trade,
        max_trade_vchf=max_trade,
        sizing_coarse_step=float(raw.get("sizing_coarse_step", 100)),
        max_sizing_quotes=int(raw.get("max_sizing_quotes", 5)),
        probe_sizes=raw.get("probe_sizes", [10, 25, 50, 100]),
        slippage_bps=int(os.getenv("SLIPPAGE_BPS", raw.get("slippage_bps", 50))),
        quote_freshness_sec=raw.get("quote_freshness_sec", 30),
        peg_min=raw.get("peg_min", 0.98),
        peg_max=raw.get("peg_max", 1.02),
        vnx_bridge_poll_sec=int(os.getenv("VNX_BRIDGE_POLL_SEC", raw.get("vnx_bridge_poll_sec", 30))),
        vnx_bridge_timeout_sec=int(
            os.getenv("VNX_BRIDGE_TIMEOUT_SEC", raw.get("vnx_bridge_timeout_sec", 3600))
        ),
        celo_gas_usd_estimate=float(
            raw.get("celo_gas_usd_estimate", raw.get("base_gas_usd_estimate", 0.25))
        ),
        celo_gas_usd_estimate=float(raw.get("celo_gas_usd_estimate", 0.25)),
        base_gas_usd_estimate=float(raw.get("base_gas_usd_estimate", 0.25)),
        solana_fee_usd_estimate=raw.get("solana_fee_usd_estimate", 0.05),
        vnx_bridge_fee_usd=raw.get(
            "vnx_bridge_fee_usd", raw.get("vnx_withdraw_fee_usd", 1.0)
        ),
        vnx_platform_fee_usd=raw.get("vnx_platform_fee_usd", 0.5),
        wormhole_bridge_fee_usd=raw.get("wormhole_bridge_fee_usd", 0.5),
        enable_vnx_arb_routes=str(os.getenv("ENABLE_VNX_ARB_ROUTES", raw.get("enable_vnx_arb_routes", True))).lower()
        in ("1", "true", "yes"),
        enable_vnx_cctp_routes=str(
            os.getenv("ENABLE_VNX_CCTP_ROUTES", raw.get("enable_vnx_cctp_routes", True))
        ).lower()
        in ("1", "true", "yes"),
        indirect_route_premium_usd=float(
            os.getenv("INDIRECT_ROUTE_PREMIUM_USD", raw.get("indirect_route_premium_usd", 5))
        ),
        eth_gas_usd_estimate=float(raw.get("eth_gas_usd_estimate", 2.0)),
        cctp_fee_usd=float(os.getenv("CCTP_FEE_USD", raw.get("cctp_fee_usd", 1.5))),
        treasury_vchf_home=str(os.getenv("TREASURY_VCHF_HOME", raw.get("treasury_vchf_home", "platform"))),
        vchf_on_chain_dust=float(os.getenv("VCHF_ON_CHAIN_DUST", raw.get("vchf_on_chain_dust", 0.5))),
        platform_vchf_only=str(
            os.getenv("PLATFORM_VCHF_ONLY", raw.get("platform_vchf_only", True))
        ).lower()
        in ("1", "true", "yes"),
        close_loop_after_cycle=str(
            os.getenv("CLOSE_LOOP_AFTER_CYCLE", raw.get("close_loop_after_cycle", True))
        ).lower()
        in ("1", "true", "yes"),
        close_loop_always_return=str(
            os.getenv("CLOSE_LOOP_ALWAYS_RETURN", raw.get("close_loop_always_return", True))
        ).lower()
        in ("1", "true", "yes"),
        close_loop_min_net_usd=float(
            os.getenv("CLOSE_LOOP_MIN_NET_USD", raw.get("close_loop_min_net_usd", 0.0))
        ),
        jit_withdraw=str(os.getenv("JIT_WITHDRAW", raw.get("jit_withdraw", True))).lower()
        in ("1", "true", "yes"),
    )


def load_bridge_config() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "bridge.yaml").read_text())


def token_decimals(token: TokenConfig, chain_key: str) -> int:
    return token.chain_decimals.get(chain_key, token.decimals)


def is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")


def db_path() -> Path:
    raw = os.getenv("DB_PATH", "data/bot.db")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Persistent state directory (parent of DB_PATH, e.g. /data on Railway)."""
    override = os.getenv("DATA_DIR", "").strip()
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = ROOT / p
    else:
        p = db_path().parent
    p.mkdir(parents=True, exist_ok=True)
    return p
