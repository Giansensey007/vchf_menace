"""One-time infinite ERC20 approvals — not part of swap/bridge hot paths."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from src.config_loader import ChainConfig, TokenConfig, is_dry_run, load_bridge_config, load_chains, load_tokens
from src.quotes.addresses import checksum

logger = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1
MAX_UINT160 = 2**160 - 1
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
SWAP_ROUTER02_ADDRESS = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
PARASWAP_PROXY = "0x216b4b4ba9f3e719726886d34a177484278bfcae"
KYBER_META_ROUTER = os.getenv("KYBER_META_ROUTER", "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5")
PERMIT2_EXPIRATION_SEC = 86400 * 365 * 10

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

PERMIT2_ABI = [
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {
                "components": [
                    {"name": "amount", "type": "uint160"},
                    {"name": "expiration", "type": "uint48"},
                    {"name": "nonce", "type": "uint48"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


class InsufficientAllowanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalTarget:
    chain_key: str
    token: str
    spender: str
    label: str


def is_infinite_allowance(allowance: int) -> bool:
    return allowance >= MAX_UINT256 // 2


def _allowance(w3, owner: str, token: str, spender: str) -> int:
    contract = w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
    return int(contract.functions.allowance(checksum(owner), checksum(spender)).call())


def allowance_error(token: str, spender: str, allowance: int, needed: int) -> str:
    return (
        f"Insufficient ERC20 allowance for {token[:10]}… → spender {spender[:10]}… "
        f"(allowance={allowance}, need>={needed}). "
        "Run: python scripts/approve_all.py  (or set AUTO_APPROVE_ON_STARTUP=true)"
    )


def check_allowance(w3, owner: str, token: str, spender: str, amount: int) -> str | None:
    allowance = _allowance(w3, owner, token, spender)
    if allowance >= amount:
        return None
    return allowance_error(token, spender, allowance, amount)


def check_permit2_allowance(w3, owner: str, token: str, spender: str, amount: int) -> str | None:
    permit2 = w3.eth.contract(address=checksum(PERMIT2_ADDRESS), abi=PERMIT2_ABI)
    current = permit2.functions.allowance(checksum(owner), checksum(token), checksum(spender)).call()
    if int(current[0]) >= amount and int(current[1]) > time.time():
        return None
    return (
        f"Insufficient Permit2 allowance for {token[:10]}… → router {spender[:10]}…. "
        "Run: python scripts/approve_all.py"
    )


def check_swap_input_allowance(w3, owner: str, token: str, router_addr: str, amount: int) -> str | None:
    router = checksum(router_addr)
    if router.lower() == checksum(SWAP_ROUTER02_ADDRESS).lower():
        err = check_allowance(w3, owner, token, PERMIT2_ADDRESS, amount)
        if err:
            return err
        return check_permit2_allowance(w3, owner, token, router, amount)
    return check_allowance(w3, owner, token, router, amount)


def _dedupe(targets: list[ApprovalTarget]) -> list[ApprovalTarget]:
    seen: set[tuple[str, str, str]] = set()
    out: list[ApprovalTarget] = []
    for t in targets:
        key = (t.chain_key, checksum(t.token).lower(), checksum(t.spender).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ApprovalTarget(
                chain_key=t.chain_key,
                token=checksum(t.token),
                spender=checksum(t.spender),
                label=t.label,
            )
        )
    return out


def _router_for_chain(chain_key: str, chain: ChainConfig) -> str | None:
    env_map = {
        "celo": "CELO_SWAP_ROUTER",
        "base": "BASE_SWAP_ROUTER",
        "ethereum": "ETH_SWAP_ROUTER",
    }
    env_key = env_map.get(chain_key)
    if env_key:
        override = os.getenv(env_key, "").strip()
        if override:
            return override
    return chain.swap_router


def collect_approval_targets(
    chains: dict[str, ChainConfig],
    token_cfg: TokenConfig,
    bridge_cfg: dict,
) -> list[ApprovalTarget]:
    targets: list[ApprovalTarget] = []
    wh = bridge_cfg["wormhole"]
    cctp = bridge_cfg.get("cctp") or {}

    for chain_key in ("celo", "base"):
        if chain_key not in chains:
            continue
        chain = chains[chain_key]
        router = _router_for_chain(chain_key, chain)
        if router:
            if chain_key in token_cfg.chains:
                targets.append(
                    ApprovalTarget(chain_key, token_cfg.chains[chain_key], router, f"{token_cfg.symbol}→DEX")
                )
            targets.append(ApprovalTarget(chain_key, chain.hub_token, router, "hub stable→DEX"))
        if getattr(chain, "kyber_slug", None) or chain_key == "base":
            kyber = os.getenv("KYBER_ROUTER_ADDRESS", KYBER_META_ROUTER)
            if chain_key in token_cfg.chains:
                targets.append(
                    ApprovalTarget(chain_key, token_cfg.chains[chain_key], kyber, f"{token_cfg.symbol}→Kyber")
                )
            targets.append(ApprovalTarget(chain_key, chain.hub_token, kyber, "hub stable→Kyber"))
        bridge = wh.get(f"{chain_key}_token_bridge")
        hub = wh.get(f"{chain_key}_usdc") or chain.hub_token
        if bridge and hub:
            targets.append(ApprovalTarget(chain_key, hub, bridge, f"USDC→Wormhole ({chain_key})"))

    if "ethereum" in chains:
        eth = chains["ethereum"]
        router = _router_for_chain("ethereum", eth)
        if router:
            targets.append(ApprovalTarget("ethereum", eth.hub_token, router, "USDC→DEX"))
            usdt = wh.get("ethereum_usdt")
            if usdt:
                targets.append(ApprovalTarget("ethereum", usdt, router, "USDT→DEX"))
            if router.lower() == checksum(SWAP_ROUTER02_ADDRESS).lower():
                for tok, lbl in (
                    (eth.hub_token, "USDC→Permit2"),
                    (wh.get("ethereum_usdt"), "USDT→Permit2"),
                ):
                    if tok:
                        targets.append(ApprovalTarget("ethereum", tok, PERMIT2_ADDRESS, lbl))
        if getattr(eth, "kyber_slug", None):
            kyber = os.getenv("KYBER_ROUTER_ADDRESS", KYBER_META_ROUTER)
            targets.append(ApprovalTarget("ethereum", eth.hub_token, kyber, "USDC→Kyber"))
            if "ethereum" in token_cfg.chains:
                targets.append(
                    ApprovalTarget("ethereum", token_cfg.chains["ethereum"], kyber, f"{token_cfg.symbol}→Kyber")
                )
        messenger = cctp.get("ethereum_token_messenger")
        if messenger:
            targets.append(ApprovalTarget("ethereum", eth.hub_token, messenger, "USDC→CCTP"))
        bridge = wh.get("ethereum_token_bridge")
        usdt = wh.get("ethereum_usdt")
        if bridge and usdt:
            targets.append(ApprovalTarget("ethereum", usdt, bridge, "USDT→Wormhole"))
        targets.append(ApprovalTarget("ethereum", eth.hub_token, PARASWAP_PROXY, "USDC→ParaSwap"))

    return _dedupe(targets)


def _executor_for_chain(chain_key: str, chains: dict[str, ChainConfig]):
    if chain_key == "celo":
        from src.execution.celo import CeloExecutor

        return CeloExecutor(chains["celo"])
    if chain_key == "base":
        from src.execution.base import BaseExecutor

        return BaseExecutor(chains["base"])
    if chain_key == "ethereum":
        from src.execution.ethereum import EthereumExecutor

        return EthereumExecutor(chains["ethereum"])
    raise ValueError(f"No executor for chain {chain_key}")


def _tx_base(executor, fn=None) -> dict:
    if hasattr(executor, "_tx_base"):
        return executor._tx_base(fn)
    return executor._base_tx(fn)


def _build_and_send(executor, tx: dict, *, fn=None) -> str | None:
    if fn is not None and hasattr(executor, "_build_and_send"):
        try:
            return executor._build_and_send(tx, fn=fn)
        except TypeError:
            pass
    return executor._build_and_send(tx)


def approve_infinite_erc20(executor, token: str, spender: str) -> str | None:
    owner = executor.account.address
    allowance = _allowance(executor.w3, owner, token, spender)
    if is_infinite_allowance(allowance):
        return "already-approved"
    if is_dry_run():
        logger.info("[DRY_RUN] approve infinite %s → %s", token[:10], spender[:10])
        return "dry-run-approve"
    contract = executor.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
    fn = contract.functions.approve(checksum(spender), MAX_UINT256)
    tx = fn.build_transaction(_tx_base(executor, fn))
    return _build_and_send(executor, tx, fn=fn)


def approve_permit2_infinite(executor, token: str, router_addr: str) -> str | None:
    owner = executor.account.address
    approve_infinite_erc20(executor, token, PERMIT2_ADDRESS)
    permit2 = executor.w3.eth.contract(address=checksum(PERMIT2_ADDRESS), abi=PERMIT2_ABI)
    current = permit2.functions.allowance(checksum(owner), checksum(token), checksum(router_addr)).call()
    if int(current[0]) >= MAX_UINT160 // 2 and int(current[1]) > time.time() + 86400:
        return "already-approved"
    if is_dry_run():
        logger.info("[DRY_RUN] Permit2 approve %s → %s", token[:10], router_addr[:10])
        return "dry-run-permit2"
    expiration = int(time.time()) + PERMIT2_EXPIRATION_SEC
    fn = permit2.functions.approve(checksum(token), checksum(router_addr), MAX_UINT160, expiration)
    tx = fn.build_transaction(_tx_base(executor, fn))
    return _build_and_send(executor, tx, fn=fn)


def ensure_infinite_approvals(
    *,
    chains: dict[str, ChainConfig] | None = None,
    token_cfg: TokenConfig | None = None,
    bridge_cfg: dict | None = None,
) -> list[dict]:
    chains = chains or load_chains()
    tokens = load_tokens()
    token_cfg = token_cfg or next(iter(tokens.values()))
    bridge_cfg = bridge_cfg or load_bridge_config()
    results: list[dict] = []

    for target in collect_approval_targets(chains, token_cfg, bridge_cfg):
        try:
            executor = _executor_for_chain(target.chain_key, chains)
        except (ValueError, Exception) as exc:
            logger.warning("Skip approval %s: %s", target.label, exc)
            continue
        logger.info("Approving %s (%s)", target.label, target.chain_key)
        tx = approve_infinite_erc20(executor, target.token, target.spender)
        results.append(
            {"chain": target.chain_key, "label": target.label, "token": target.token, "spender": target.spender, "tx": tx}
        )

    if "ethereum" in chains:
        eth = chains["ethereum"]
        router = _router_for_chain("ethereum", eth)
        if router and checksum(router).lower() == checksum(SWAP_ROUTER02_ADDRESS).lower():
            try:
                executor = _executor_for_chain("ethereum", chains)
                wh = bridge_cfg["wormhole"]
                for tok in (eth.hub_token, wh.get("ethereum_usdt")):
                    if tok:
                        tx = approve_permit2_infinite(executor, tok, router)
                        results.append({"chain": "ethereum", "label": f"Permit2 {tok[:10]}…", "tx": tx})
            except Exception as exc:
                logger.warning("Permit2 approvals failed: %s", exc)

    return results


def skip_token_approvals() -> bool:
    return os.getenv("SKIP_TOKEN_APPROVALS", "false").lower() in ("1", "true", "yes")


def auto_approve_on_startup() -> bool:
    return os.getenv("AUTO_APPROVE_ON_STARTUP", "false").lower() in ("1", "true", "yes")


def run_startup_approvals() -> None:
    if skip_token_approvals():
        logger.info("SKIP_TOKEN_APPROVALS=true — skipping startup token approvals")
        return
    if auto_approve_on_startup():
        logger.info("AUTO_APPROVE_ON_STARTUP=true — ensuring infinite token approvals")
        ensure_infinite_approvals()
    else:
        logger.info(
            "Token approvals not run at startup (set AUTO_APPROVE_ON_STARTUP=true or run scripts/approve_all.py)"
        )
