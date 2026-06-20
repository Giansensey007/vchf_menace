from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from src.config_loader import BotConfig, ChainConfig, TokenConfig, is_dry_run, token_decimals
from src.execution.base import BaseExecutor
from src.execution.celo import CeloExecutor
from src.execution.executor import ArbExecutor, CycleRecord, CycleState
from src.execution.solana import SolanaExecutor
from src.quotes.types import from_human, to_human
from src.scanner.simulator import simulate_cctp_usdc_return_to_vnx, simulate_direction, simulate_round_trip
from src.treasury.in_flight import (
    InFlightLedger,
    PendingVnxWithdraw,
    format_treasury_balance_line,
    parse_vnx_withdrawals,
)
from src.treasury.loops import (
    DIRECTIONS_FROM_ORIGIN,
    closes_to_origin,
    inverse_direction,
    leg_end,
    return_closes_origin_with_cctp,
    return_leg_direction,
    use_cctp_usdc_return,
)
from src.vnx.bridge import VnxBridge
from src.vnx.client import VnxClient
from src.vnx.deposits import check_deposit_amount, min_deposit_vchf
from src.vnx.trading import platform_buy_vchf

logger = logging.getLogger(__name__)


@dataclass
class TreasurySnapshot:
    platform_vchf: float = 0.0
    platform_usdc: float = 0.0
    platform_chf: float = 0.0
    base_vchf: float = 0.0
    base_usdc: float = 0.0
    celo_vchf: float = 0.0
    celo_usdt: float = 0.0
    sol_vchf: float = 0.0
    sol_usdc: float = 0.0
    pending_vnx_withdraws: list[PendingVnxWithdraw] = field(default_factory=list)
    in_flight_summary: str = ""


@dataclass
class PrepareResult:
    ready: bool
    direction: str
    size_vchf: float
    notes: list[str] = field(default_factory=list)
    consolidated_vchf: float = 0.0


@dataclass
class ClosedLoopResult:
    origin: str
    primary_direction: str
    primary: CycleRecord | None
    return_direction: str | None
    return_leg: CycleRecord | None
    closed: bool
    reason: str
    round_trip_profit_usd: float | None = None


class TreasuryManager:
    """
    Platform-centric VCHF treasury: idle VCHF lives on VNX only.
    Chains hold hub stables (Base USDC, Celo USDT, Sol USDC) for buy legs.
    """

    def __init__(
        self,
        chains: dict[str, ChainConfig],
        token: TokenConfig,
        bot_cfg: BotConfig,
    ) -> None:
        self.chains = chains
        self.token = token
        self.cfg = bot_cfg
        self.dust = bot_cfg.vchf_on_chain_dust
        self._ledger = InFlightLedger("VCHF")

    def balance_line(self, snap: TreasurySnapshot) -> str:
        return format_treasury_balance_line(
            snap,
            "vchf",
            pending_vnx_withdraws=snap.pending_vnx_withdraws,
            in_flight_summary=snap.in_flight_summary,
        )

    def _platform_vchf_only(self) -> bool:
        return self.cfg.platform_vchf_only and self.cfg.treasury_vchf_home == "platform"

    async def assert_vchf_home_policy(self) -> tuple[bool, str]:
        """Verify on-chain VCHF is at or below dust threshold."""
        if not self._platform_vchf_only():
            return True, "policy off"
        snap = await self.snapshot()
        over = []
        pending_base = self._ledger.total_pending_to_blockchain("BASE")
        pending_celo = self._ledger.total_pending_to_blockchain("CELO")
        pending_sol = self._ledger.total_pending_to_blockchain("SOL")
        base_adj = max(0.0, snap.base_vchf - pending_base)
        celo_adj = max(0.0, snap.celo_vchf - pending_celo)
        sol_adj = max(0.0, snap.sol_vchf - pending_sol)
        if base_adj > self.dust:
            over.append(f"base={snap.base_vchf:.2f}")
        if celo_adj > self.dust:
            over.append(f"celo={snap.celo_vchf:.2f}")
        if sol_adj > self.dust:
            over.append(f"sol={snap.sol_vchf:.2f}")
        if over:
            pending_note = ""
            if pending_base or pending_celo or pending_sol:
                pending_note = (
                    f" (pending withdraw base={pending_base:.2f} "
                    f"celo={pending_celo:.2f} sol={pending_sol:.2f})"
                )
            return False, f"on-chain VCHF above dust ({self.dust}): {', '.join(over)}{pending_note}"
        return True, "ok"

    async def snapshot(self) -> TreasurySnapshot:
        snap = TreasurySnapshot()
        api_withdrawals: list[PendingVnxWithdraw] = []
        async with VnxClient() as vnx:
            bal = await vnx.account_balance()
            snap.platform_vchf = vnx.vchf_balance(bal)
            snap.platform_usdc = vnx.usdc_balance(bal)
            snap.platform_chf = vnx.chf_balance(bal)
            wd_resp = await vnx.query_withdrawals()
            if wd_resp:
                api_withdrawals.extend(parse_vnx_withdrawals(wd_resp, "VCHF"))
            tr_resp = await vnx.query_transfers()
            if tr_resp:
                api_withdrawals.extend(parse_vnx_withdrawals(tr_resp, "VCHF"))

        base = BaseExecutor(self.chains["base"])
        bdec = token_decimals(self.token, "base")
        snap.base_vchf = float(to_human(base.balance_erc20(self.token.chains["base"]), bdec))
        snap.base_usdc = float(
            to_human(base.balance_erc20(self.chains["base"].hub_token), self.chains["base"].hub_decimals)
        )

        if "celo" in self.chains and "celo" in self.token.chains:
            celo = CeloExecutor(self.chains["celo"])
            cdec = token_decimals(self.token, "celo")
            snap.celo_vchf = float(to_human(celo.balance_erc20(self.token.chains["celo"]), cdec))
            snap.celo_usdt = float(
                to_human(celo.balance_erc20(self.chains["celo"].hub_token), self.chains["celo"].hub_decimals)
            )

        sol = SolanaExecutor(self.chains["solana"])
        from spl.token.instructions import get_associated_token_address
        from solders.pubkey import Pubkey

        vchf_ata = get_associated_token_address(
            sol.keypair.pubkey(), Pubkey.from_string(self.token.chains["solana"])
        )
        usdc_ata = get_associated_token_address(
            sol.keypair.pubkey(), Pubkey.from_string(self.chains["solana"].hub_token)
        )
        try:
            snap.sol_vchf = sol.token_balance_ui(vchf_ata)
        except Exception:
            snap.sol_vchf = 0.0
        try:
            snap.sol_usdc = sol.token_balance_ui(usdc_ata)
        except Exception:
            snap.sol_usdc = 0.0

        self._ledger.reconcile(
            platform_token=snap.platform_vchf,
            base_token=snap.base_vchf,
            celo_token=snap.celo_vchf,
            sol_token=snap.sol_vchf,
            api_withdrawals=api_withdrawals or None,
        )
        snap.pending_vnx_withdraws = api_withdrawals + [
            PendingVnxWithdraw(
                asset=r.asset,
                quantity=r.quantity,
                blockchain=r.blockchain,
                destination=r.destination,
                status=r.status,
                txid=r.txids[0] if r.txids else None,
                created_at=r.created_at,
            )
            for r in self._ledger.pending_vnx_withdraws()
        ]
        snap.in_flight_summary = self._ledger.format_summary()
        return snap

    async def consolidate_vchf_to_platform(self) -> float:
        """Deposit on-chain VCHF dust/excess to VNX (platform home). Returns total moved."""
        snap = await self.snapshot()
        moved = 0.0
        bridge = VnxBridge(self.cfg)

        bc = os.getenv("VNX_BASE_BLOCKCHAIN", "BASE")
        base_min = min_deposit_vchf(bc)
        if snap.base_vchf > self.dust and snap.base_vchf < base_min:
            logger.warning(
                "Stuck Base VCHF %.4f: above dust (%.2f) but below VNX min deposit (%.2f)",
                snap.base_vchf,
                self.dust,
                base_min,
            )
        if snap.base_vchf >= base_min:
            qty = snap.base_vchf
            dep_err = check_deposit_amount(bc, qty)
            if dep_err:
                logger.warning("Skip Base VCHF consolidate (%.4f): %s", qty, dep_err)
            else:
                logger.info("Treasury: deposit %.4f VCHF from Base → platform", qty)
                base = BaseExecutor(self.chains["base"])
                dec = token_decimals(self.token, "base")

                async def base_dep(addr: str) -> str | None:
                    return base.transfer_erc20(
                        self.token.chains["base"], addr, from_human(qty, dec)
                    )

                br = await bridge.bridge_vchf(
                    direction="treasury_base_to_platform",
                    quantity=qty,
                    source_blockchain=bc,
                    dest_blockchain=bc,
                    dest_label="platform",
                    deposit_tx_builder=base_dep,
                    deposit_only=True,
                )
                if br.success:
                    moved += qty
                else:
                    logger.warning("Base VCHF consolidate failed: %s", br.error)

        snap = await self.snapshot()
        celo_bc = os.getenv("VNX_CELO_BLOCKCHAIN", "CELO")
        celo_min = min_deposit_vchf(celo_bc)
        if snap.celo_vchf > self.dust and snap.celo_vchf < celo_min:
            logger.warning(
                "Stuck Celo VCHF %.4f: above dust (%.2f) but below VNX min deposit (%.2f)",
                snap.celo_vchf,
                self.dust,
                celo_min,
            )
        if snap.celo_vchf >= celo_min:
            qty = snap.celo_vchf
            dep_err = check_deposit_amount(celo_bc, qty)
            if dep_err:
                logger.warning("Skip Celo VCHF consolidate (%.4f): %s", qty, dep_err)
            else:
                logger.info("Treasury: deposit %.4f VCHF from Celo → platform", qty)
                celo = CeloExecutor(self.chains["celo"])
                dec = token_decimals(self.token, "celo")

                async def celo_dep(addr: str) -> str | None:
                    return celo.transfer_erc20(
                        self.token.chains["celo"], addr, from_human(qty, dec)
                    )

                br = await bridge.bridge_vchf(
                    direction="treasury_celo_to_platform",
                    quantity=qty,
                    source_blockchain=celo_bc,
                    dest_blockchain=celo_bc,
                    dest_label="platform",
                    deposit_tx_builder=celo_dep,
                    deposit_only=True,
                )
                if br.success:
                    moved += qty
                else:
                    logger.warning("Celo VCHF consolidate failed: %s", br.error)

        snap = await self.snapshot()
        sol_bc = os.getenv("VNX_SOL_BLOCKCHAIN", "SOL")
        sol_min = min_deposit_vchf(sol_bc)
        if snap.sol_vchf > self.dust and snap.sol_vchf < sol_min:
            logger.warning(
                "Stuck Sol VCHF %.4f: above dust (%.2f) but below VNX min deposit (%.2f)",
                snap.sol_vchf,
                self.dust,
                sol_min,
            )
        if snap.sol_vchf >= sol_min:
            qty = snap.sol_vchf
            dep_err = check_deposit_amount(sol_bc, qty)
            if dep_err:
                logger.warning("Skip Sol VCHF consolidate (%.4f): %s", qty, dep_err)
            else:
                logger.info("Treasury: deposit %.4f VCHF from Sol → platform", qty)
                sol = SolanaExecutor(self.chains["solana"])
                dec = token_decimals(self.token, "solana")

                async def sol_dep(addr: str) -> str | None:
                    return sol.transfer_spl(
                        self.token.chains["solana"], addr, from_human(qty, dec), dec
                    )

                br = await bridge.bridge_vchf(
                    direction="treasury_sol_to_platform",
                    quantity=qty,
                    source_blockchain=sol_bc,
                    dest_blockchain=sol_bc,
                    dest_label="platform",
                    deposit_tx_builder=sol_dep,
                    deposit_only=True,
                )
                if br.success:
                    moved += qty
                else:
                    logger.warning("Sol VCHF consolidate failed: %s", br.error)

        if moved > 0:
            logger.info("Treasury consolidated %.4f VCHF to platform", moved)
        return moved

    async def prepare_for_direction(self, direction: str, size_vchf: float) -> PrepareResult:
        """JIT prep: sweep VCHF home, verify stables / platform inventory for the leg."""
        notes: list[str] = []
        consolidated = 0.0
        if self.cfg.jit_withdraw or self._platform_vchf_only():
            consolidated = await self.consolidate_vchf_to_platform()
            if consolidated:
                notes.append(f"consolidated {consolidated:.2f} VCHF to platform")

        snap = await self.snapshot()

        for p in self._ledger.pending_vnx_withdraws():
            notes.append(
                f"{p.quantity:.2f} VCHF pending {p.blockchain} withdraw since {p.created_at[:19]}"
            )

        if self._platform_vchf_only():
            ok, msg = await self.assert_vchf_home_policy()
            if not ok:
                notes.append(msg)

        if direction in ("celo_to_solana", "solana_to_celo"):
            if not await self.ensure_platform_vchf_for_bridge(size_vchf):
                notes.append(f"platform VCHF short for bridge ({snap.platform_vchf:.1f} < {size_vchf:.0f})")
                return PrepareResult(False, direction, size_vchf, notes, consolidated)

        if direction.startswith("vnx_to_"):
            from src.vnx.bridge import VCHF_WITHDRAW_FEE_BUFFER
            from src.vnx.trading import VCHF_MIN_ORDER, _round_down, VCHF_USDC_QTY_DECIMALS

            chain_key = "celo" if direction.endswith("celo") else "solana"
            dest_bc = os.getenv(
                "VNX_CELO_BLOCKCHAIN" if chain_key == "celo" else "VNX_SOL_BLOCKCHAIN",
                "CELO" if chain_key == "celo" else "SOL",
            )
            pending = self._ledger.pending_for_blockchain(dest_bc)
            if pending:
                total_pending = sum(p.quantity for p in pending)
                notes.append(
                    f"awaiting in-flight VNX withdraw {total_pending:.2f} VCHF to {dest_bc} "
                    "(will poll on-chain, not double-withdraw)"
                )
            withdrawable = max(0.0, snap.platform_vchf - VCHF_WITHDRAW_FEE_BUFFER)
            if snap.platform_vchf >= size_vchf * 0.95:
                pass
            elif withdrawable >= 1.0:
                # Withdraw has no 30 VCHF floor — only platform buy/sell does.
                size_vchf = _round_down(withdrawable, VCHF_USDC_QTY_DECIMALS)
                notes.append(f"withdraw-only size {size_vchf:.2f} VCHF (platform balance minus fee buffer)")
            else:
                need_usdc = VCHF_MIN_ORDER * 1.35
                if snap.platform_usdc < need_usdc * 0.95:
                    notes.append(
                        f"platform short: need withdrawable VCHF≥{size_vchf:.0f} or USDC≥{need_usdc:.0f} "
                        f"to buy {VCHF_MIN_ORDER:.0f} VCHF (platform order min; have "
                        f"{snap.platform_vchf:.1f} VCHF, {snap.platform_usdc:.1f} USDC)"
                    )
                    return PrepareResult(False, direction, size_vchf, notes, consolidated)
                size_vchf = VCHF_MIN_ORDER
                notes.append(f"will buy {size_vchf:.0f} VCHF on platform (order minimum)")

        if direction in ("celo_to_vnx", "celo_to_solana"):
            need_usdt = size_vchf * 1.35
            if snap.celo_usdt < need_usdt * 0.9:
                notes.append(
                    f"Celo needs ≥{need_usdt:.0f} USDT (have {snap.celo_usdt:.1f}) — "
                    "fund via vnx_to_celo or wormhole"
                )
                return PrepareResult(False, direction, size_vchf, notes, consolidated)

        if direction in ("solana_to_vnx", "solana_to_celo"):
            need_usdc = size_vchf * 1.35
            if snap.sol_usdc < need_usdc * 0.9:
                notes.append(
                    f"Sol needs ≥{need_usdc:.0f} USDC (have {snap.sol_usdc:.1f}) — fund via vnx_to_solana"
                )
                return PrepareResult(False, direction, size_vchf, notes, consolidated)

        notes.append("ready")
        return PrepareResult(True, direction, size_vchf, notes, consolidated)

    async def ensure_platform_vchf_for_bridge(self, size_vchf: float) -> bool:
        """Buy VCHF on platform if needed for cross-chain bridge inventory."""
        snap = await self.snapshot()
        if snap.platform_vchf >= size_vchf * 0.95:
            return True
        need = size_vchf - snap.platform_vchf
        if snap.platform_usdc < need * 1.2:
            logger.warning(
                "Cannot top-up platform VCHF (need %.1f, USDC %.1f)", need, snap.platform_usdc
            )
            return False
        buy = await platform_buy_vchf(self.cfg, need, max_usdc=snap.platform_usdc * 0.995)
        return buy.success

    async def run_closed_loop(
        self,
        client,
        executor: ArbExecutor,
        *,
        origin: str,
        direction: str,
        size_vchf: float,
        force_return: bool | None = None,
        force_execute: bool = False,
    ) -> ClosedLoopResult:
        """
        Execute `direction` then return capital to `origin` hub stable.

        When close_loop_always_return: always run inverse leg (capital homing).
        Otherwise: return only if inverse sim profitable or round-trip ≥ min_net.
        """
        force = force_return if force_return is not None else _env_bool("CLOSE_LOOP_FORCE", False)
        always_return = force or self.cfg.close_loop_always_return
        min_round = self.cfg.close_loop_min_net_usd

        prep = await self.prepare_for_direction(direction, size_vchf)
        if not prep.ready:
            return ClosedLoopResult(
                origin, direction, None, None, None, False, "; ".join(prep.notes)
            )

        exec_size = prep.size_vchf
        primary = await executor.run_cycle(
            client, direction, exec_size, force_execute=force_execute or always_return
        )
        await self.consolidate_vchf_to_platform()

        if primary.state != CycleState.DONE:
            return ClosedLoopResult(
                origin, direction, primary, None, None, False, primary.error or "primary failed"
            )

        if closes_to_origin(origin, direction):
            return ClosedLoopResult(
                origin,
                direction,
                primary,
                None,
                None,
                True,
                "primary already ends on origin",
                primary.simulation.net_profit_usd if primary.simulation else None,
            )

        inv = return_leg_direction(origin, direction, enable_cctp=self.cfg.enable_vnx_cctp_routes)
        if not inv:
            end = leg_end(direction)
            return ClosedLoopResult(
                origin,
                direction,
                primary,
                None,
                None,
                False,
                f"no inverse leg; capital on {end}",
            )

        if not return_closes_origin_with_cctp(
            origin, direction, enable_cctp=self.cfg.enable_vnx_cctp_routes
        ):
            return ClosedLoopResult(
                origin,
                direction,
                primary,
                inv,
                None,
                False,
                f"return {inv} does not close to {origin}",
            )

        if use_cctp_usdc_return(origin, direction, enable_cctp=self.cfg.enable_vnx_cctp_routes):
            usdc_on_sol = primary.simulation.stable_out_usd if primary.simulation else 0.0
            inv_sim = await simulate_cctp_usdc_return_to_vnx(
                client, self.chains, self.token, self.cfg, usdc_on_sol, exec_size
            )
        else:
            inv_sim = await simulate_direction(
                client, self.chains, self.token, self.cfg, inv, exec_size
            )
        primary_profit = primary.simulation.net_profit_usd if primary.simulation else 0.0
        round_profit = primary_profit + inv_sim.net_profit_usd
        return_profitable = inv_sim.profitable and inv_sim.net_profit_usd >= self.cfg.min_profit_usd
        round_ok = round_profit >= min_round and (return_profitable or round_profit >= self.cfg.min_profit_usd)

        if not always_return and not return_profitable and not round_ok:
            await self.consolidate_vchf_to_platform()
            return ClosedLoopResult(
                origin,
                direction,
                primary,
                inv,
                None,
                False,
                f"return leg uneconomic (inv profit ${inv_sim.net_profit_usd:.2f}, "
                f"round ${round_profit:.2f}) — capital left at {leg_end(direction)}",
                round_profit,
            )

        if not return_profitable and always_return:
            logger.warning(
                "Running return leg %s despite sim loss $%.2f (close_loop_always_return)",
                inv,
                inv_sim.net_profit_usd,
            )

        prep_ret = await self.prepare_for_direction(inv, exec_size)
        if (
            not prep_ret.ready
            and not always_return
            and not use_cctp_usdc_return(origin, direction, enable_cctp=self.cfg.enable_vnx_cctp_routes)
        ):
            return ClosedLoopResult(
                origin,
                direction,
                primary,
                inv,
                None,
                False,
                f"return prep failed: {prep_ret.notes}",
                round_profit,
            )

        return_size = exec_size
        if primary.simulation and primary.simulation.token_mid > 0:
            return_size = primary.simulation.token_mid

        if use_cctp_usdc_return(origin, direction, enable_cctp=self.cfg.enable_vnx_cctp_routes):
            usdc_amt = primary.simulation.stable_out_usd if primary.simulation else 0.0
            return_record = await executor.run_cctp_usdc_return_to_vnx(
                client,
                usdc_amt,
                return_size,
                force_execute=force_execute or always_return,
            )
        else:
            return_record = await executor.run_cycle(
                client, inv, return_size, force_execute=force_execute or always_return
            )
        await self.consolidate_vchf_to_platform()

        closed = return_record.state == CycleState.DONE
        return ClosedLoopResult(
            origin,
            direction,
            primary,
            inv,
            return_record,
            closed,
            "closed loop" if closed else (return_record.error or "return failed"),
            round_profit if closed else round_profit,
        )

    async def best_closed_loop_from_origin(
        self, client, executor: ArbExecutor, origin: str, size_vchf: float
    ) -> ClosedLoopResult | None:
        """Pick best profitable direction from origin and run as closed loop."""
        candidates = DIRECTIONS_FROM_ORIGIN.get(origin, ())
        best_dir: str | None = None
        best_profit = float("-inf")
        for d in candidates:
            rt = await simulate_round_trip(
                client, self.chains, self.token, self.cfg, d, size_vchf, origin=origin
            )
            if not rt.profitable:
                continue
            if rt.round_trip_profit_usd > best_profit:
                best_profit = rt.round_trip_profit_usd
                best_dir = d
        if not best_dir:
            return None
        return await self.run_closed_loop(
            client, executor, origin=origin, direction=best_dir, size_vchf=size_vchf
        )


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes")
