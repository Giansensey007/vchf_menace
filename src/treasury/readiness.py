"""Production funding readiness vs config/production.yaml targets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.config_loader import CONFIG_DIR, load_bridge_config, load_chains, load_tokens, token_decimals
from src.execution.base import BaseExecutor
from src.execution.ethereum import ERC20_ABI, EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.types import to_human
from src.vnx.client import VnxClient


@dataclass
class FundingTarget:
    key: str
    label: str
    target: float
    actual: float
    unit: str

    @property
    def gap(self) -> float:
        return max(0.0, self.target - self.actual)

    @property
    def ok(self) -> bool:
        return self.actual >= self.target * 0.95


def load_production_config() -> dict[str, Any]:
    path = CONFIG_DIR / "production.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw or {}


def production_targets(section: str = "production") -> dict[str, float]:
    raw = load_production_config().get(section) or {}
    return {k: float(v) for k, v in raw.items()}


async def collect_balances() -> dict[str, float]:
    chains = load_chains()
    token = load_tokens()["VCHF"]
    wh = load_bridge_config()["wormhole"]
    out: dict[str, float] = {}

    async with VnxClient() as vnx:
        bal = await vnx.account_balance()
        out["platform_vchf"] = vnx.vchf_balance(bal)
        out["platform_usdc"] = vnx.usdc_balance(bal)

    celo = BaseExecutor(chains["base"])
    out["base_usdc"] = float(to_human(celo.balance_erc20(chains["base"].hub_token), 6))
    out["base_native"] = float(celo.w3.from_wei(celo.w3.eth.get_balance(celo.address), "ether"))
    dec = token_decimals(token, "base")
    out["celo_vchf"] = float(to_human(celo.balance_erc20(token.chains["base"]), dec))

    wrapped = wh.get("base_usdc_wormhole_from_eth")
    if wrapped:
        out["base_usdc_wrapped_eth"] = float(
            to_human(
                celo.w3.eth.contract(address=celo.w3.to_checksum_address(wrapped), abi=ERC20_ABI)
                .functions.balanceOf(celo.address)
                .call(),
                6,
            )
        )

    sol = SolanaExecutor(chains["solana"])
    from spl.token.instructions import get_associated_token_address
    from solders.pubkey import Pubkey

    usdc_ata = get_associated_token_address(
        sol.keypair.pubkey(), Pubkey.from_string(chains["solana"].hub_token)
    )
    out["sol_usdc"] = sol.token_balance_ui(usdc_ata)
    out["sol_native"] = sol.balance_lamports() / 1e9

    eth = EthereumExecutor(chains["ethereum"])
    out["eth_usdc"] = float(to_human(eth.balance_erc20(chains["ethereum"].hub_token), 6))
    out["eth_usdt"] = float(to_human(eth.balance_erc20(wh["ethereum_usdt"]), 6))
    out["eth_native"] = float(eth.w3.from_wei(eth.w3.eth.get_balance(eth.address), "ether"))

    return out


async def funding_report(section: str = "production") -> tuple[list[FundingTarget], dict[str, float]]:
    targets = production_targets(section)
    balances = await collect_balances()

    labels = {
        "platform_vchf": "VNX VCHF",
        "platform_usdc": "VNX USDC",
        "base_usdc": "Base USDT (canonical)",
        "sol_usdc": "Sol USDC",
        "eth_native": "ETH gas",
        "eth_usdc": "ETH USDC (hub)",
        "eth_usdt": "ETH USDT (hub)",
        "base_native": "BASE gas",
        "sol_native": "SOL gas",
    }
    units = {
        "platform_vchf": "VCHF",
        "platform_usdc": "USDC",
        "base_usdc": "USDT",
        "sol_usdc": "USDC",
        "eth_native": "ETH",
        "eth_usdc": "USDC",
        "eth_usdt": "USDT",
        "base_native": "BASE",
        "sol_native": "SOL",
    }

    rows: list[FundingTarget] = []
    for key, target in targets.items():
        actual = balances.get(key, 0.0)
        rows.append(
            FundingTarget(
                key=key,
                label=labels.get(key, key),
                target=target,
                actual=actual,
                unit=units.get(key, ""),
            )
        )
    return rows, balances


def format_report(rows: list[FundingTarget], balances: dict[str, float]) -> str:
    lines = ["=== Production funding readiness ==="]
    for row in rows:
        status = "OK" if row.ok else f"NEED +{row.gap:.2f}"
        lines.append(
            f"  {'OK' if row.ok else '!!'} {row.label:<24} "
            f"{row.actual:>10.2f} / {row.target:.2f} {row.unit}  ({status})"
        )
    wrapped = balances.get("base_usdc_wrapped_eth")
    if wrapped is not None and wrapped > 0.01:
        lines.append(f"  -- Base wrapped ETH-USDT (Wormhole): {wrapped:.2f} USDT (not canonical)")
    ready = all(r.ok for r in rows)
    lines.append(f"\n{'PRODUCTION READY' if ready else 'UNDER-FUNDED — see gaps above'}")
    return "\n".join(lines)
