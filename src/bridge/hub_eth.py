from __future__ import annotations

import logging
import os

from src.config_loader import BotConfig, load_bot_config, load_bridge_config, load_chains
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.types import from_human, to_human
from src.vnx.deposits import check_usdc_deposit_amount
from src.vnx.usdc_bridge import VnxUsdcBridge

logger = logging.getLogger(__name__)


async def eth_usdc_to_vnx(client, amount_usdc: float, bot_cfg: BotConfig | None = None) -> dict:
    """ETH hot wallet USDC → VNX platform deposit."""
    dep_err = check_usdc_deposit_amount("ETH", amount_usdc)
    if dep_err:
        logger.error("Aborting ETH USDC→VNX deposit: %s", dep_err)
        return {
            "success": False,
            "direction": "eth_to_vnx",
            "amount_usdc": amount_usdc,
            "deposit_tx": None,
            "error": dep_err,
        }

    cfg = bot_cfg or load_bot_config()
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    amount_raw = from_human(amount_usdc, chains["ethereum"].hub_decimals)

    async def builder(addr: str) -> str | None:
        return eth.transfer_erc20(chains["ethereum"].hub_token, addr, amount_raw)

    bridge = VnxUsdcBridge(cfg)
    br = await bridge.deposit_usdc(amount_usdc, deposit_tx_builder=builder)
    return {
        "success": br.success,
        "direction": "eth_to_vnx",
        "amount_usdc": amount_usdc,
        "deposit_tx": br.deposit_tx,
        "error": br.error,
    }


async def vnx_usdc_to_eth(client, amount_usdc: float, bot_cfg: BotConfig | None = None) -> dict:
    """VNX platform USDC → ETH hot wallet withdraw."""
    cfg = bot_cfg or load_bot_config()
    bridge = VnxUsdcBridge(cfg)
    br = await bridge.withdraw_usdc(amount_usdc)
    return {
        "success": br.success,
        "direction": "vnx_to_eth",
        "amount_usdc": br.quantity,
        "withdraw_txids": br.withdraw_txids,
        "error": br.error,
    }


logger = logging.getLogger(__name__)


def _simulate_eth_swap(eth: EthereumExecutor, token_in: str, token_out: str, amount_human: float) -> tuple[dict | None, int]:
    """Find a Uniswap V3 fee tier that quotes for the ETH stable pair."""
    amount_in = from_human(amount_human, 6)
    for fee in (100, 500, 3000, 10000):
        sim = eth.simulate_swap(token_in, token_out, amount_in, fee=fee)
        if sim:
            return sim, fee
    # Quoter eth_call often fails on public RPCs; stable pairs ~1:1 on mainnet
    logger.warning("ETH quoter unavailable — using 1:1 stable fallback for %.4f", amount_human)
    return {"amount_in": amount_in, "amount_out": amount_in, "provider": "stable_fallback"}, 100


async def swap_eth_usdt_to_usdc(amount_usdt: float, *, slippage_bps: int = 50) -> dict:
    """Swap Ethereum USDT → USDC via Uniswap V3 (post-Wormhole landing)."""
    chains = load_chains()
    wh = load_bridge_config()["wormhole"]
    eth = EthereumExecutor(chains["ethereum"])
    usdt = wh["ethereum_usdt"]
    usdc = chains["ethereum"].hub_token
    sim, fee = _simulate_eth_swap(eth, usdt, usdc, amount_usdt)
    amount_in = from_human(amount_usdt, 6)
    min_out = int(sim["amount_out"] * (1 - slippage_bps / 10_000))
    tx = eth.swap_exact_input(usdt, usdc, amount_in, min_out, fee=fee)
    return {
        "success": bool(tx),
        "tx": tx,
        "amount_in_usdt": amount_usdt,
        "expected_usdc": float(to_human(sim["amount_out"], 6)),
        "error": eth.last_error if not tx else None,
    }


async def swap_eth_usdc_to_usdt(amount_usdc: float, *, slippage_bps: int = 50) -> dict:
    """Swap Ethereum USDC → USDT before Wormhole initiate to Celo."""
    chains = load_chains()
    wh = load_bridge_config()["wormhole"]
    eth = EthereumExecutor(chains["ethereum"])
    usdt = wh["ethereum_usdt"]
    usdc = chains["ethereum"].hub_token
    sim, fee = _simulate_eth_swap(eth, usdc, usdt, amount_usdc)
    amount_in = from_human(amount_usdc, 6)
    min_out = int(sim["amount_out"] * (1 - slippage_bps / 10_000))
    tx = eth.swap_exact_input(usdc, usdt, amount_in, min_out, fee=fee)
    return {
        "success": bool(tx),
        "tx": tx,
        "amount_in_usdc": amount_usdc,
        "expected_usdt": float(to_human(sim["amount_out"], 6)),
        "error": eth.last_error if not tx else None,
    }


async def celo_usdt_to_vnx_usdc(
    client,
    amount_usdt: float,
    bot_cfg: BotConfig | None = None,
    *,
    slippage_bps: int | None = None,
) -> dict:
    """
    CELO USDT → ETH USDT (Wormhole + redeem) → ETH USDC (Uniswap) → VNX platform deposit.
    """
    from src.bridge.wormhole import WormholePortalBridge

    cfg = bot_cfg or load_bot_config()
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    slippage = slippage_bps if slippage_bps is not None else cfg.slippage_bps
    wh = WormholePortalBridge(chains["celo"])

    br = await wh.bridge_usdt_with_redeem(
        client,
        from_chain="celo",
        to_chain="ethereum",
        amount_usdt=amount_usdt,
        recipient=eth.address,
        intent="celo_usdt_to_vnx",
    )
    if not br.success:
        return {"success": False, "stage": "wormhole", "wormhole": br, "error": br.error}

    swap = await swap_eth_usdt_to_usdc(amount_usdt * 0.995, slippage_bps=slippage)
    if not swap["success"]:
        return {"success": False, "stage": "swap", "wormhole": br, "error": swap.get("error")}

    usdc_out = swap.get("expected_usdc") or amount_usdt * 0.99
    dep_err = check_usdc_deposit_amount("ETH", usdc_out)
    if dep_err:
        logger.error("Aborting CELO→VNX USDC deposit (post-swap %.2f USDC): %s", usdc_out, dep_err)
        return {
            "success": False,
            "stage": "deposit",
            "wormhole": br,
            "swap_tx": swap.get("tx"),
            "error": dep_err,
        }
    deposit = await eth_usdc_to_vnx(client, usdc_out, cfg)
    return {
        "success": deposit["success"],
        "stage": "deposit",
        "wormhole": br,
        "swap_tx": swap.get("tx"),
        "deposit": deposit,
        "error": deposit.get("error"),
    }


async def wormhole_celo_to_eth(client, amount_usdt: float) -> dict:
    """CELO USDT → ETH USDT with automated VAA redeem."""
    from src.bridge.wormhole import WormholePortalBridge

    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    wh = WormholePortalBridge(chains["celo"])
    br = await wh.bridge_usdt_with_redeem(
        client,
        from_chain="celo",
        to_chain="ethereum",
        amount_usdt=amount_usdt,
        recipient=eth.address,
        intent="celo_to_ethereum_usdt",
    )
    return {"success": br.success, "wormhole": br, "error": br.error}


async def wormhole_eth_to_celo(client, amount_usdt: float) -> dict:
    """ETH USDT → CELO USDT with automated VAA redeem."""
    from src.bridge.wormhole import WormholePortalBridge

    chains = load_chains()
    celo = chains["celo"]
    from src.execution.celo import CeloExecutor

    celo_exec = CeloExecutor(celo)
    wh = WormholePortalBridge(celo)
    br = await wh.bridge_usdt_with_redeem(
        client,
        from_chain="ethereum",
        to_chain="celo",
        amount_usdt=amount_usdt,
        recipient=celo_exec.address,
        intent="ethereum_to_celo_usdt",
    )
    return {"success": br.success, "wormhole": br, "error": br.error}


async def wormhole_eth_to_celo_via_usdc(client, amount_usdc: float, bot_cfg: BotConfig | None = None) -> dict:
    """Platform/ETH USDC → USDT swap → Wormhole ETH→Celo USDT."""
    cfg = bot_cfg or load_bot_config()
    swap = await swap_eth_usdc_to_usdt(amount_usdc * 0.998, slippage_bps=cfg.slippage_bps)
    if not swap["success"]:
        return {"success": False, "stage": "swap", "error": swap.get("error")}
    usdt = swap.get("expected_usdt") or amount_usdc * 0.995
    wh = await wormhole_eth_to_celo(client, usdt)
    return {"success": wh["success"], "swap_tx": swap.get("tx"), "wormhole": wh.get("wormhole"), "error": wh.get("error")}


async def celo_usdt_to_sol_usdc(
    client,
    amount_usdt: float,
    bot_cfg: BotConfig | None = None,
    *,
    slippage_bps: int | None = None,
) -> dict:
    """
    CELO USDT → ETH USDT (Wormhole) → ETH USDC (Uniswap) → Sol USDC (Circle CCTP).

    Hub triangle leg for rebalancing Celo stable inventory to Solana without VCHF.
    """
    from src.bridge.cctp import CircleCctpBridge

    cfg = bot_cfg or load_bot_config()
    slippage = slippage_bps if slippage_bps is not None else cfg.slippage_bps

    wh = await wormhole_celo_to_eth(client, amount_usdt)
    if not wh["success"]:
        return {
            "success": False,
            "direction": "celo_usdt_to_sol_usdc",
            "stage": "wormhole_celo_eth",
            "wormhole": wh.get("wormhole"),
            "error": wh.get("error"),
        }

    usdt_on_eth = amount_usdt * 0.995
    swap = await swap_eth_usdt_to_usdc(usdt_on_eth, slippage_bps=slippage)
    if not swap["success"]:
        return {
            "success": False,
            "direction": "celo_usdt_to_sol_usdc",
            "stage": "swap_eth_usdt_usdc",
            "wormhole": wh.get("wormhole"),
            "error": swap.get("error"),
        }

    usdc_out = swap.get("expected_usdc") or usdt_on_eth * 0.998
    bridge = CircleCctpBridge()
    cctp = await bridge.bridge_usdc_eth_to_sol(client, usdc_out)
    return {
        "success": cctp.success,
        "direction": "celo_usdt_to_sol_usdc",
        "stage": "cctp_eth_sol" if cctp.success else "cctp_eth_sol_pending",
        "wormhole": wh.get("wormhole"),
        "swap_tx": swap.get("tx"),
        "cctp": cctp,
        "error": cctp.error,
    }


async def sol_usdc_to_celo_usdt(
    client,
    amount_usdc: float,
    bot_cfg: BotConfig | None = None,
    *,
    slippage_bps: int | None = None,
) -> dict:
    """
    Sol USDC → ETH USDC (CCTP) → ETH USDT (Uniswap) → CELO USDT (Wormhole).

    Inverse hub triangle — Sol stable inventory back to Celo via Ethereum.
    """
    from src.bridge.cctp import CircleCctpBridge

    cfg = bot_cfg or load_bot_config()
    slippage = slippage_bps if slippage_bps is not None else cfg.slippage_bps

    bridge = CircleCctpBridge()
    cctp = await bridge.bridge_usdc_sol_to_eth(client, amount_usdc)
    if not cctp.success and not cctp.dest_tx:
        return {
            "success": False,
            "direction": "sol_usdc_to_celo_usdt",
            "stage": "cctp_sol_eth",
            "cctp": cctp,
            "error": cctp.error,
        }

    usdc_on_eth = amount_usdc * 0.995 if cctp.success else amount_usdc * 0.99
    swap = await swap_eth_usdc_to_usdt(usdc_on_eth, slippage_bps=slippage)
    if not swap["success"]:
        return {
            "success": False,
            "direction": "sol_usdc_to_celo_usdt",
            "stage": "swap_eth_usdc_usdt",
            "cctp": cctp,
            "error": swap.get("error"),
        }

    usdt_out = swap.get("expected_usdt") or usdc_on_eth * 0.998
    wh = await wormhole_eth_to_celo(client, usdt_out)
    return {
        "success": wh["success"],
        "direction": "sol_usdc_to_celo_usdt",
        "stage": "wormhole_eth_celo" if wh["success"] else "wormhole_eth_celo_pending",
        "cctp": cctp,
        "swap_tx": swap.get("tx"),
        "wormhole": wh.get("wormhole"),
        "error": wh.get("error"),
    }


async def eth_usdt_to_sol_usdc(
    client,
    amount_usdt: float,
    bot_cfg: BotConfig | None = None,
    *,
    slippage_bps: int | None = None,
) -> dict:
    """ETH USDT → ETH USDC (Uniswap) → Sol USDC (CCTP). Completes CELO→ETH→SOL after Wormhole redeem."""
    from src.bridge.cctp import CircleCctpBridge

    cfg = bot_cfg or load_bot_config()
    slippage = slippage_bps if slippage_bps is not None else cfg.slippage_bps
    swap = await swap_eth_usdt_to_usdc(amount_usdt, slippage_bps=slippage)
    if not swap["success"]:
        return {"success": False, "direction": "eth_usdt_to_sol_usdc", "stage": "swap", "error": swap.get("error")}
    usdc_out = swap.get("expected_usdc") or amount_usdt * 0.998
    bridge = CircleCctpBridge()
    cctp = await bridge.bridge_usdc_eth_to_sol(client, usdc_out)
    return {
        "success": cctp.success,
        "direction": "eth_usdt_to_sol_usdc",
        "stage": "cctp_eth_sol" if cctp.success else "cctp_eth_sol_pending",
        "swap_tx": swap.get("tx"),
        "cctp": cctp,
        "error": cctp.error,
    }


async def fund_eth_gas_from_usdc(amount_usdc: float = 10.0, *, slippage_bps: int = 100) -> dict:
    """Swap ETH-wallet USDC → native ETH for gas (~$10 default)."""
    chains = load_chains()
    eth = EthereumExecutor(chains["ethereum"])
    native_before = float(eth.w3.from_wei(eth.balance_native(), "ether"))
    tx = eth.swap_usdc_to_native_eth(amount_usdc, slippage_bps=slippage_bps)
    via = "uniswap" if tx else None
    if not tx:
        tx = eth.swap_usdc_to_native_eth_paraswap(amount_usdc, slippage_bps=slippage_bps)
        via = "paraswap" if tx else None
    native_after = float(eth.w3.from_wei(eth.balance_native(), "ether"))
    return {
        "success": bool(tx),
        "tx": tx,
        "amount_usdc": amount_usdc,
        "eth_native_after": native_after,
        "eth_native_before": native_before,
        "error": eth.last_error if not tx else None,
        "via": via,
    }


async def fund_sol_gas_from_usdc(
    client,
    amount_usdc: float = 10.0,
    *,
    slippage_bps: int = 50,
) -> dict:
    """CCTP ETH USDC → Sol USDC, then Jupiter USDC → SOL for gas."""
    from src.bridge.cctp import CircleCctpBridge

    chains = load_chains()
    sol = SolanaExecutor(chains["solana"])
    bridge = CircleCctpBridge()
    cctp_amount = amount_usdc + 1.5  # CCTP fee buffer
    cctp = await bridge.bridge_usdc_eth_to_sol(client, cctp_amount)
    if not cctp.success:
        return {
            "success": False,
            "stage": "cctp",
            "cctp": cctp,
            "error": cctp.error,
        }

    amount_raw = from_human(amount_usdc, 6)
    sol_mint = "So11111111111111111111111111111111111111112"
    sig = await sol.swap(
        client,
        chains["solana"].hub_token,
        sol_mint,
        amount_raw,
        slippage_bps=slippage_bps,
    )
    return {
        "success": bool(sig),
        "stage": "jupiter",
        "cctp": cctp,
        "swap_tx": sig,
        "sol_native_after": sol.balance_lamports() / 1e9,
        "error": None if sig else "jupiter swap failed",
    }


async def fund_celo_gas_from_usdc(
    client,
    amount_usdc: float = 10.0,
    bot_cfg: BotConfig | None = None,
) -> dict:
    """
    ETH USDC → USDT → Wormhole → Celo USDT, then swap USDT → CELO on Celo.

    Skips if native CELO already exceeds ~$10 equivalent (no USDT/CELO pool on Uniswap Celo).
    """
    from src.execution.celo import CeloExecutor

    cfg = bot_cfg or load_bot_config()
    chains = load_chains()
    celo = CeloExecutor(chains["celo"])
    celo_native = float(celo.w3.from_wei(celo.balance_native(), "ether"))
    celo_usd = float(os.getenv("CELO_USD_ESTIMATE", "0.35"))
    if celo_native * celo_usd >= amount_usdc * 0.9:
        return {
            "success": True,
            "skipped": True,
            "reason": f"celo_native {celo_native:.2f} CELO already >= ${amount_usdc:.0f} gas target",
            "celo_native": celo_native,
        }

    wh = await wormhole_eth_to_celo_via_usdc(client, amount_usdc, cfg)
    if not wh["success"]:
        return {"success": False, "stage": "wormhole", "error": wh.get("error"), "wormhole": wh}

    celo_erc20 = "0x471EcE3750Da23735093b24508Ea98577cD1679"
    wh_cfg = load_bridge_config()["wormhole"]
    usdt_token = wh_cfg["celo_usdt_wormhole_from_eth"]
    amount_in = from_human(amount_usdc * 0.95, 6)
    min_out = int(amount_usdc / celo_usd * 0.9 * 1e18)
    tx = None
    for fee in (500, 3000, 100):
        tx = celo.swap_exact_input(usdt_token, celo_erc20, amount_in, min_out, fee=fee)
        if tx:
            break
    native_after = float(celo.w3.from_wei(celo.balance_native(), "ether"))
    return {
        "success": bool(tx),
        "skipped": False,
        "wormhole": wh,
        "swap_tx": tx,
        "celo_native_after": native_after,
        "error": None if tx else "celo USDT→CELO swap failed (no pool?)",
    }


async def fund_all_chain_gas(
    client,
    amount_usdc_per_chain: float = 10.0,
    *,
    withdraw_from_vnx: bool = True,
    bot_cfg: BotConfig | None = None,
) -> dict:
    """
    Use platform USDC to fund ~$10 native gas on ETH, SOL, and CELO.

    Withdraws ~3× amount (+ CCTP buffer) from VNX to ETH first.
    """
    cfg = bot_cfg or load_bot_config()
    results: dict = {"amount_per_chain": amount_usdc_per_chain, "steps": {}}

    if withdraw_from_vnx:
        needed = amount_usdc_per_chain * 3 + 2.0  # CCTP + swap buffers
        wd = await vnx_usdc_to_eth(client, needed, cfg)
        results["steps"]["vnx_withdraw"] = wd
        if not wd["success"]:
            results["success"] = False
            results["error"] = wd.get("error")
            return results
        import asyncio
        import time

        chains = load_chains()
        eth = EthereumExecutor(chains["ethereum"])
        target = from_human(needed * 0.95, 6)
        deadline = time.time() + cfg.vnx_bridge_timeout_sec
        while time.time() < deadline:
            await asyncio.sleep(15)
            bal = eth.balance_erc20(chains["ethereum"].hub_token)
            if bal >= target:
                break
        results["steps"]["eth_usdc_after_withdraw"] = float(to_human(eth.balance_erc20(chains["ethereum"].hub_token), 6))

    eth_gas = await fund_eth_gas_from_usdc(amount_usdc_per_chain)
    results["steps"]["eth_gas"] = eth_gas

    sol_gas = await fund_sol_gas_from_usdc(client, amount_usdc_per_chain, slippage_bps=cfg.slippage_bps)
    results["steps"]["sol_gas"] = sol_gas

    celo_gas = await fund_celo_gas_from_usdc(client, amount_usdc_per_chain, cfg)
    results["steps"]["celo_gas"] = celo_gas

    results["success"] = all(
        s.get("success")
        for k, s in results["steps"].items()
        if k not in ("vnx_withdraw", "eth_usdc_after_withdraw")
    )
    return results


async def wormhole_celo_to_sol_direct(client, amount_usdt: float) -> dict:
    """CELO USDT → Sol USDT (Wormhole Portal, single hop — no ETH)."""
    from src.bridge.wormhole import WormholePortalBridge
    from src.execution.solana import SolanaExecutor

    chains = load_chains()
    sol = SolanaExecutor(chains["solana"])
    wh = WormholePortalBridge(chains["celo"])
    br = await wh.bridge_usdt_with_redeem(
        client,
        from_chain="celo",
        to_chain="solana",
        amount_usdt=amount_usdt,
        recipient=sol.pubkey,
        intent="celo_to_solana_usdt",
    )
    return {"success": br.success, "direction": "celo_to_solana_usdt", "wormhole": br, "error": br.error}
