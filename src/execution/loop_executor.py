"""Execute platform-first same-asset loops (Loop 1/2/3) step by step.

Each loop is a same-asset round trip produced by ``src.scanner.routes.LoopSpec``
and sized/quoted by ``src.scanner.loop_simulator.simulate_loop``. This executor
walks the loop's steps and dispatches every ``StepKind`` to the existing live
primitives (VNX withdraw/deposit, on-chain swaps, CCTP/Wormhole stable bridges,
platform buy/sell). The loop-closing buy-back legs are the only buys the bot
ever makes.

Safety model (matches the rest of the project):
- DRY_RUN is the default deploy mode; under dry-run every primitive returns a
  dry-run result and inter-leg balance polling is skipped, so the full sequence
  runs offline and is unit-tested.
- Live execution (DRY_RUN off) is gated behind ``cfg.enable_loop_executor``
  (``ENABLE_LOOP_EXECUTOR``) so it cannot fire until explicitly enabled and
  live-validated.

VCHF trades on Celo (USDT), Base (USDC) and Solana (USDC); ETH is the USDC
settlement hub. Celo legs bridge via Wormhole; Base/Solana/ETH legs via CCTP.
Celo<->Base is an ETH-triangle pair and is not yet wired for live execution.

Additive: this does not yet replace the legacy directed-route executor.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx

from src.bridge.cctp import CircleCctpBridge
from src.bridge.hub_eth import eth_usdc_to_vnx
from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import (
    BotConfig,
    ChainConfig,
    TokenConfig,
    is_dry_run,
    load_bot_config,
    load_bridge_config,
    token_decimals,
)
from src.db import log_cycle_step
from src.execution.base import BaseExecutor
from src.execution.celo import CeloExecutor
from src.execution.ethereum import EthereumExecutor
from src.execution.solana import SolanaExecutor
from src.quotes.types import from_human
from src.scanner.loop_simulator import LoopSimulation, simulate_loop
from src.scanner.routes import LOOP1_OUTBOUND, LOOP2_INBOUND, LOOP3_CROSS, LoopSpec
from src.vnx.bridge import VnxBridge
from src.vnx.deposits import validate_eth_usdc_vnx_deposit
from src.vnx.trading import platform_buy_vchf, platform_sell_vchf
from src.vnx.usdc_bridge import VnxUsdcBridge

logger = logging.getLogger(__name__)

# EVM chains share one executor interface (swap_exact_input/transfer_erc20/...).
# Resolve by name through the module namespace so tests can patch the classes.
_EVM_EXEC_NAMES = {"celo": "CeloExecutor", "base": "BaseExecutor", "ethereum": "EthereumExecutor"}

_BC_ENV: dict[str, tuple[str, str]] = {
    "celo": ("VNX_CELO_BLOCKCHAIN", "CELO"),
    "solana": ("VNX_SOL_BLOCKCHAIN", "SOL"),
    "base": ("VNX_BASE_BLOCKCHAIN", "BASE"),
    "ethereum": ("VNX_ETH_BLOCKCHAIN", "ETH"),
}
_LABEL_ENV: dict[str, tuple[str, str]] = {
    "celo": ("VNX_CELO_WITHDRAW_LABEL", "celo-hot"),
    "solana": ("VNX_SOL_WITHDRAW_LABEL", "sol-hot"),
    "base": ("VNX_BASE_WITHDRAW_LABEL", "base-hot"),
    "ethereum": ("VNX_ETH_WITHDRAW_LABEL", "arb_explorer_mainnet_USDC"),
}

_CCTP_METHODS: dict[tuple[str, str], str] = {
    ("solana", "ethereum"): "bridge_usdc_sol_to_eth",
    ("ethereum", "solana"): "bridge_usdc_eth_to_sol",
    ("base", "ethereum"): "bridge_usdc_base_to_eth",
    ("ethereum", "base"): "bridge_usdc_eth_to_base",
    ("base", "solana"): "bridge_usdc_base_to_sol",
    ("solana", "base"): "bridge_usdc_sol_to_base",
}


class LoopState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"


@dataclass
class LoopRecord:
    id: str
    loop_key: str
    family: str
    size: float
    state: LoopState = LoopState.IDLE
    simulation: LoopSimulation | None = None
    tx_hashes: list[str] = field(default_factory=list)
    steps_done: list[str] = field(default_factory=list)
    token_out: float = 0.0
    error: str | None = None


class LoopExecutor:
    """Run a single same-asset loop end to end."""

    def __init__(
        self,
        chains: dict[str, ChainConfig],
        token: TokenConfig,
        bot_cfg: BotConfig | None = None,
    ) -> None:
        self.chains = chains
        self.token = token
        self.cfg = bot_cfg or load_bot_config()
        self.slip = self.cfg.slippage_bps / 10000.0

    # ---- helpers ---------------------------------------------------------
    def _bc(self, chain: str) -> str:
        env, default = _BC_ENV[chain]
        return os.getenv(env, default)

    def _label(self, chain: str) -> str:
        env, default = _LABEL_ENV[chain]
        return os.getenv(env, default)

    def _evm(self, chain: str):
        cls = globals()[_EVM_EXEC_NAMES[chain]]
        return cls(self.chains[chain])

    def _chain_addr(self, chain: str) -> str:
        if chain in _EVM_EXEC_NAMES and chain in self.chains:
            return self._evm(chain).address
        if chain == "solana":
            return os.getenv("SOLANA_PUBLIC_KEY", "")
        return ""

    @staticmethod
    def _leg_usd(sim: LoopSimulation, kind: str, default: float = 0.0) -> float:
        for leg in sim.legs:
            if leg.kind == kind:
                return leg.usd_after
        return default

    def _fail(self, rec: LoopRecord, msg: str | None) -> bool:
        rec.state = LoopState.FAILED
        rec.error = (msg or "loop step failed")[:300]
        if rec.steps_done and not is_dry_run():
            logger.error(
                "LOOP %s FAILED mid-flight — MANUAL RECOVERY may be needed (funds may be on-chain "
                "or in-bridge). loop=%s size=%s steps_done=%s txs=%s err=%s",
                rec.id, rec.loop_key, rec.size, rec.steps_done, rec.tx_hashes, rec.error,
            )
        return False

    # ---- entry point -----------------------------------------------------
    async def run_loop(
        self,
        client: httpx.AsyncClient,
        loop: LoopSpec,
        size: float,
        *,
        force_execute: bool = False,
    ) -> LoopRecord:
        rec = LoopRecord(
            id=str(uuid.uuid4())[:8], loop_key=loop.key, family=loop.family, size=size
        )
        sim = await simulate_loop(client, self.chains, self.token, self.cfg, loop, size)
        rec.simulation = sim
        if not force_execute:
            if not sim.profitable:
                return self._failed_record(rec, sim.error or "loop not profitable")
            if sim.net_profit_usd < self.cfg.min_profit_usd:
                return self._failed_record(
                    rec, f"profit ${sim.net_profit_usd:.2f} below min ${self.cfg.min_profit_usd:.2f}"
                )
        elif sim.error and not sim.profitable:
            logger.warning("Force-executing loop %s despite sim: %s", loop.key, sim.error)

        if not is_dry_run() and not self.cfg.enable_loop_executor:
            return self._failed_record(
                rec,
                "live loop execution disabled — set ENABLE_LOOP_EXECUTOR=1 after live validation "
                "(deploy with DRY_RUN first)",
            )

        rec.state = LoopState.RUNNING
        log_cycle_step(
            rec.id, "loop_quote",
            {"loop": loop.key, "size": size, "net_profit_usd": sim.net_profit_usd, "dry_run": is_dry_run()},
        )
        try:
            if loop.family == LOOP1_OUTBOUND:
                await self._run_loop1(client, rec, loop, sim)
            elif loop.family == LOOP2_INBOUND:
                await self._run_loop2(client, rec, loop, sim)
            elif loop.family == LOOP3_CROSS:
                await self._run_loop3(client, rec, loop, sim)
            else:
                self._fail(rec, f"unknown loop family {loop.family}")
        except Exception as exc:  # noqa: BLE001 — surface as record failure
            self._fail(rec, str(exc))
            logger.exception("Loop %s failed", rec.id)
        return rec

    def _failed_record(self, rec: LoopRecord, msg: str) -> LoopRecord:
        rec.state = LoopState.FAILED
        rec.error = msg
        return rec

    # ---- loop families ---------------------------------------------------
    async def _run_loop1(
        self, client: httpx.AsyncClient, rec: LoopRecord, loop: LoopSpec, sim: LoopSimulation
    ) -> None:
        x = loop.chain_a
        got = await self._withdraw_token(rec, x, rec.size)
        if got is None:
            return
        sell_usd = self._leg_usd(sim, "sell_onchain", default=rec.size)
        if not await self._sell_onchain(client, rec, x, got, sell_usd):
            return
        usd = sell_usd
        if x != loop.hub:
            mech = self._loop_mechanism(loop)
            if not await self._bridge_stable(client, rec, x, loop.hub, usd, mech):
                return
            usd = self._leg_usd(sim, "bridge_stable", default=usd)
            if not await self._await_stable(rec, loop.hub, usd):
                return
        if not await self._vnx_usdc_deposit(client, rec, usd):
            return
        buy_usd = self._leg_usd(sim, "vnx_usdc_deposit", default=usd)
        if not await self._platform_buyback(rec, sim.token_out, buy_usd):
            return
        rec.token_out = sim.token_out
        rec.state = LoopState.DONE

    async def _run_loop2(
        self, client: httpx.AsyncClient, rec: LoopRecord, loop: LoopSpec, sim: LoopSimulation
    ) -> None:
        x = loop.chain_a
        if not await self._platform_sell(rec, rec.size):
            return
        usd = self._leg_usd(sim, "platform_sell", default=rec.size)
        if not await self._withdraw_usdc(rec, usd):
            return
        if not await self._await_stable(rec, loop.hub, usd):
            return
        if x != loop.hub:
            mech = self._loop_mechanism(loop)
            if not await self._bridge_stable(client, rec, loop.hub, x, usd, mech):
                return
            usd = self._leg_usd(sim, "bridge_stable", default=usd)
            if not await self._await_stable(rec, x, usd):
                return
        token_out = await self._buyback_onchain(client, rec, x, usd, sim.token_out)
        if token_out is None:
            return
        if not await self._deposit_token(rec, x, token_out):
            return
        rec.token_out = sim.token_out
        rec.state = LoopState.DONE

    async def _run_loop3(
        self, client: httpx.AsyncClient, rec: LoopRecord, loop: LoopSpec, sim: LoopSimulation
    ) -> None:
        a, b = loop.chain_a, loop.chain_b
        assert b is not None
        got = await self._withdraw_token(rec, a, rec.size)
        if got is None:
            return
        sell_usd = self._leg_usd(sim, "sell_onchain", default=rec.size)
        if not await self._sell_onchain(client, rec, a, got, sell_usd):
            return
        usd = sell_usd
        mech = self._loop_mechanism(loop)
        if not await self._bridge_stable(client, rec, a, b, usd, mech):
            return
        usd = self._leg_usd(sim, "bridge_stable", default=usd)
        if not await self._await_stable(rec, b, usd):
            return
        token_out = await self._buyback_onchain(client, rec, b, usd, sim.token_out)
        if token_out is None:
            return
        if not await self._deposit_token(rec, b, token_out):
            return
        rec.token_out = sim.token_out
        rec.state = LoopState.DONE

    @staticmethod
    def _loop_mechanism(loop: LoopSpec) -> str | None:
        return next((s.mechanism for s in loop.bridge_legs), None)

    # ---- step primitives -------------------------------------------------
    async def _withdraw_token(self, rec: LoopRecord, chain: str, qty: float) -> float | None:
        bridge = VnxBridge(self.cfg)
        br = await bridge.bridge_vchf(
            direction=f"vnx_to_{chain}",
            quantity=qty,
            source_blockchain=self._bc(chain),
            dest_blockchain=self._bc(chain),
            dest_label=self._label(chain),
            deposit_tx_builder=lambda _addr: None,
            withdraw_only=True,
        )
        if not br.success:
            self._fail(rec, br.error or f"withdraw {self.token.symbol} to {chain} failed")
            return None
        if br.withdraw_txids:
            rec.tx_hashes.extend(str(t) for t in br.withdraw_txids if t)
        got = br.quantity or qty
        await self._await_token(chain, got)
        rec.steps_done.append("withdraw_token")
        return got

    async def _sell_onchain(
        self, client: httpx.AsyncClient, rec: LoopRecord, chain: str, qty: float, expect_usd: float
    ) -> bool:
        dec = token_decimals(self.token, chain)
        if chain == "solana":
            ex = SolanaExecutor(self.chains["solana"])

            def do_swap():
                return ex.swap(
                    client, self.token.chains["solana"], self.chains["solana"].hub_token,
                    from_human(qty, dec), self.cfg.slippage_bps,
                )
        elif chain in _EVM_EXEC_NAMES:
            ex = self._evm(chain)
            min_out = int(expect_usd * (1 - self.slip) * 10 ** self.chains[chain].hub_decimals)

            def do_swap():
                return ex.swap_exact_input(
                    self.token.chains[chain], self.chains[chain].hub_token, from_human(qty, dec), min_out
                )
        else:
            return self._fail(rec, f"on-chain sell not supported on {chain}")
        tx = await self._swap_with_retry(f"sell {self.token.symbol} on {chain}", do_swap)
        if not tx:
            return self._fail(rec, f"{chain} sell {self.token.symbol} failed")
        rec.tx_hashes.append(tx)
        rec.steps_done.append("sell_token_onchain")
        log_cycle_step(rec.id, "sell_token_onchain", {"chain": chain, "tx": tx})
        return True

    async def _buyback_onchain(
        self, client: httpx.AsyncClient, rec: LoopRecord, chain: str, usd: float, target_out: float
    ) -> float | None:
        dec = token_decimals(self.token, chain)
        hub_dec = self.chains[chain].hub_decimals
        if chain == "solana":
            ex = SolanaExecutor(self.chains["solana"])

            def do_swap():
                return ex.swap(
                    client, self.chains["solana"].hub_token, self.token.chains["solana"],
                    from_human(usd, hub_dec), self.cfg.slippage_bps,
                )
        elif chain in _EVM_EXEC_NAMES:
            ex = self._evm(chain)
            min_token = int(target_out * (1 - self.slip) * 10 ** dec)

            def do_swap():
                return ex.swap_exact_input(
                    self.chains[chain].hub_token, self.token.chains[chain], from_human(usd, hub_dec), min_token
                )
        else:
            self._fail(rec, f"on-chain buy-back not supported on {chain}")
            return None
        tx = await self._swap_with_retry(f"buy-back {self.token.symbol} on {chain}", do_swap)
        if not tx:
            self._fail(rec, f"{chain} buy-back {self.token.symbol} failed")
            return None
        rec.tx_hashes.append(tx)
        rec.steps_done.append("onchain_buyback")
        log_cycle_step(rec.id, "onchain_buyback", {"chain": chain, "tx": tx})
        return target_out

    async def _deposit_token(self, rec: LoopRecord, chain: str, qty: float) -> bool:
        dec = token_decimals(self.token, chain)
        if chain == "solana":
            ex = SolanaExecutor(self.chains["solana"])

            async def builder(addr: str) -> str | None:
                return ex.transfer_spl(self.token.chains["solana"], addr, from_human(qty, dec), dec)
        elif chain in _EVM_EXEC_NAMES:
            ex = self._evm(chain)

            async def builder(addr: str) -> str | None:
                return ex.transfer_erc20(self.token.chains[chain], addr, from_human(qty, dec))
        else:
            return self._fail(rec, f"token deposit not supported from {chain}")
        bridge = VnxBridge(self.cfg)
        br = await bridge.bridge_vchf(
            direction=f"{chain}_to_vnx",
            quantity=qty,
            source_blockchain=self._bc(chain),
            dest_blockchain=self._bc(chain),
            dest_label="platform",
            deposit_tx_builder=builder,
            deposit_only=True,
        )
        if not br.success:
            return self._fail(rec, br.error or f"deposit {self.token.symbol} from {chain} failed")
        if br.deposit_tx:
            rec.tx_hashes.append(br.deposit_tx)
        rec.steps_done.append("vnx_token_deposit")
        return True

    async def _platform_sell(self, rec: LoopRecord, qty: float) -> bool:
        sell = await platform_sell_vchf(self.cfg, qty)
        if not sell.success:
            return self._fail(rec, sell.error or "platform sell failed")
        rec.steps_done.append("platform_sell_token")
        log_cycle_step(
            rec.id, "platform_sell_token",
            {"quantity": sell.quantity, "price": sell.price, "ordid": sell.ordid, "dry_run": sell.dry_run},
        )
        return True

    async def _platform_buyback(self, rec: LoopRecord, qty: float, max_usd: float) -> bool:
        buy = await platform_buy_vchf(self.cfg, qty, max_usdc=max_usd if max_usd > 0 else None)
        if not buy.success:
            return self._fail(rec, buy.error or "platform buy-back failed")
        rec.steps_done.append("platform_buyback")
        log_cycle_step(
            rec.id, "platform_buyback",
            {"quantity": buy.quantity, "price": buy.price, "ordid": buy.ordid, "dry_run": buy.dry_run},
        )
        return True

    async def _withdraw_usdc(self, rec: LoopRecord, usd: float) -> bool:
        br = await VnxUsdcBridge(self.cfg).withdraw_usdc(usd, direction="vnx_to_eth")
        if not br.success:
            return self._fail(rec, br.error or "USDC withdraw to ETH failed")
        if br.withdraw_txids:
            rec.tx_hashes.extend(str(t) for t in br.withdraw_txids if t)
        rec.steps_done.append("withdraw_usdc")
        return True

    async def _vnx_usdc_deposit(self, client: httpx.AsyncClient, rec: LoopRecord, usd: float) -> bool:
        dep_err = validate_eth_usdc_vnx_deposit(usd)
        if dep_err:
            return self._fail(rec, dep_err)
        dep = await eth_usdc_to_vnx(client, usd, self.cfg)
        if not dep.get("success"):
            return self._fail(rec, dep.get("error") or "ETH USDC deposit to VNX failed")
        if dep.get("deposit_tx"):
            rec.tx_hashes.append(dep["deposit_tx"])
        rec.steps_done.append("vnx_usdc_deposit")
        return True

    async def _bridge_stable(
        self, client: httpx.AsyncClient, rec: LoopRecord, frm: str, to: str, usd: float, mechanism: str | None
    ) -> bool:
        if mechanism in (None, "none"):
            return True  # same chain / ETH-as-hub special case: USDC already on ETH
        if mechanism == "cctp":
            cctp = CircleCctpBridge()
            name = _CCTP_METHODS.get((frm, to))
            method = getattr(cctp, name, None) if name else None
            if method is None:
                return self._fail(rec, f"no CCTP route {frm}->{to}")
            br = await method(client, usd)
            source_domain, dest_domain = self._cctp_domains(frm, to)
        elif mechanism == "wormhole":
            br = await self._wormhole_call(client, frm, to, usd)
            source_domain = dest_domain = None
        else:
            return self._fail(rec, f"{mechanism} bridge {frm}->{to} not wired for live execution yet")
        log_cycle_step(
            rec.id, "bridge_stable",
            {"from": frm, "to": to, "mechanism": mechanism, "success": br.success, "dry_run": br.dry_run},
        )
        if not br.success:
            return self._fail(rec, br.error or f"{mechanism} bridge {frm}->{to} failed")
        if getattr(br, "source_tx", None):
            rec.tx_hashes.append(br.source_tx)
        if (
            mechanism == "cctp"
            and not is_dry_run()
            and getattr(br, "source_tx", None)
            and not getattr(br, "dest_tx", None)
            and source_domain is not None
        ):
            from src.bridge.cctp_queue import CctpClaimQueue

            queue = CctpClaimQueue()
            queue.enqueue(
                source_tx=br.source_tx, source_domain=source_domain,
                dest_domain=dest_domain, intent=f"loop_bridge_{frm}_{to}",
            )
            summary = await queue.run_until_empty(
                client, interval_sec=15.0, max_rounds=40, discover_first=False
            )
            if summary.get("claimed", 0) < 1:
                return self._fail(rec, f"CCTP claim {frm}->{to} timed out")
        rec.steps_done.append(f"bridge_{frm}_{to}")
        return True

    def _cctp_domains(self, frm: str, to: str) -> tuple[int | None, int | None]:
        try:
            cfg = load_bridge_config()["cctp"]
            key = {"ethereum": "ethereum_domain", "solana": "solana_domain", "base": "base_domain"}
            return int(cfg[key[frm]]), int(cfg[key[to]])
        except Exception:  # noqa: BLE001
            return None, None

    async def _wormhole_call(self, client: httpx.AsyncClient, frm: str, to: str, usd: float):
        """Initiate AND redeem the Wormhole transfer so the stable actually lands on `to`.

        Uses bridge_usdt_with_redeem, which enqueues the burn and runs the VAA claim
        queue until the destination receives. Returns failure (so the loop aborts
        cleanly) if the VAA cannot be claimed in time, instead of racing the buy-back.
        """
        recipient = self._chain_addr(to)
        if not recipient and not is_dry_run():
            raise ValueError(f"no recipient address for Wormhole dest {to}")
        wh = WormholePortalBridge(self.chains["celo"])
        return await wh.bridge_usdt_with_redeem(
            client,
            from_chain=frm,
            to_chain=to,
            amount_usdt=usd,
            recipient=recipient,
            intent=f"loop_bridge_{frm}_{to}",
        )

    async def _swap_with_retry(self, label: str, do_swap):
        """Run a swap thunk with bounded retries on transient failure.

        ``do_swap`` returns a tx hash (truthy) on success or ``None`` on failure;
        it may be sync (EVM) or return an awaitable (Solana). A reverted/failed
        swap leaves no state change, so re-attempting is safe.
        """
        attempts = max(1, self.cfg.loop_swap_retry_max + 1)
        last_err: str | None = None
        for i in range(attempts):
            try:
                res = do_swap()
                if asyncio.iscoroutine(res):
                    res = await res
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                res = None
            if res:
                if i:
                    logger.info("loop swap %s ok on attempt %s/%s", label, i + 1, attempts)
                return res
            if i + 1 < attempts:
                delay = min(2 ** i, 10)
                logger.warning(
                    "loop swap %s failed (attempt %s/%s)%s; retrying in %.0fs",
                    label, i + 1, attempts, f": {last_err}" if last_err else "", delay,
                )
                if not is_dry_run():
                    await asyncio.sleep(delay)
        logger.error("loop swap %s failed after %s attempts: %s", label, attempts, last_err)
        return None

    async def _await_token(self, chain: str, qty: float) -> None:
        """Live-only: wait for withdrawn token to credit the chain wallet."""
        if is_dry_run():
            return
        dec = token_decimals(self.token, chain)
        needed = from_human(qty * 0.99, dec)
        deadline = time.time() + self.cfg.vnx_bridge_timeout_sec
        while time.time() < deadline:
            try:
                if self._token_balance_raw(chain) >= needed:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.cfg.vnx_bridge_poll_sec)
        raise RuntimeError(f"timeout waiting for {self.token.symbol} on {chain} after VNX withdraw")

    async def _await_stable(self, rec: LoopRecord, chain: str, usd: float) -> bool:
        """Live-only: wait for bridged/withdrawn hub stable to credit `chain` before
        the next swap/deposit consumes it. Returns False (and fails the loop) on timeout.

        Chains without an on-chain balance reader are skipped (settlement owned by
        the downstream primitive)."""
        if is_dry_run() or usd <= 0:
            return True
        if self._stable_balance_raw(chain) is None:
            return True  # no on-chain reader; downstream primitive owns settlement
        hub_dec = self.chains[chain].hub_decimals
        # allow for bridge fee / rounding: require ~97% of the simulated amount
        needed = from_human(usd * 0.97, hub_dec)
        deadline = time.time() + self.cfg.vnx_bridge_timeout_sec
        while time.time() < deadline:
            try:
                bal = self._stable_balance_raw(chain)
                if bal is not None and bal >= needed:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.cfg.vnx_bridge_poll_sec)
        return self._fail(rec, f"timeout waiting for hub stable on {chain} after bridge/withdraw")

    def _stable_balance_raw(self, chain: str) -> int | None:
        if chain in _EVM_EXEC_NAMES:
            return self._evm(chain).balance_erc20(self.chains[chain].hub_token)
        if chain == "solana":
            from solders.pubkey import Pubkey
            from spl.token.instructions import get_associated_token_address

            sol = SolanaExecutor(self.chains["solana"])
            ata = get_associated_token_address(
                sol.keypair.pubkey(), Pubkey.from_string(self.chains["solana"].hub_token)
            )
            return int(sol.token_account_balance(ata).value.amount)
        return None

    def _token_balance_raw(self, chain: str) -> int:
        if chain in _EVM_EXEC_NAMES:
            return self._evm(chain).balance_erc20(self.token.chains[chain])
        if chain == "solana":
            from solders.pubkey import Pubkey
            from spl.token.instructions import get_associated_token_address

            sol = SolanaExecutor(self.chains["solana"])
            ata = get_associated_token_address(
                sol.keypair.pubkey(), Pubkey.from_string(self.token.chains["solana"])
            )
            return int(sol.token_account_balance(ata).value.amount)
        return 0
