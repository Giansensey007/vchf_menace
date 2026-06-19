from __future__ import annotations

import enum
import logging
import time
import uuid
import asyncio
from dataclasses import dataclass, field

import os

import httpx

from src.bridge.hub_eth import eth_usdc_to_vnx
from src.vnx.deposits import check_usdc_deposit_amount
from src.bridge.hub_usdt import usdc_raw_for_solana_buy
from src.bridge.cctp import CircleCctpBridge
from src.bridge.wormhole import WormholePortalBridge
from src.config_loader import BotConfig, ChainConfig, TokenConfig, is_dry_run, load_bot_config, load_bridge_config, token_decimals
from src.db import log_cycle_step, save_cycle
from src.execution.celo import CeloExecutor
from src.execution.sol_rpc import SOL_BALANCE_POLL_SEC
from src.execution.solana import SolanaExecutor
from src.quotes.types import from_human, to_human
from src.scanner.routes import CCTP_SOL_USDC_TO_VNX, route_for_direction
from src.scanner.simulator import CycleSimulation, simulate_cctp_usdc_return_to_vnx, simulate_direction
from src.vnx.bridge import VnxBridge
from src.vnx.client import VnxClient
from src.vnx.trading import platform_buy_vchf, platform_sell_vchf

logger = logging.getLogger(__name__)


class CycleState(enum.Enum):
    IDLE = "idle"
    QUOTING = "quoting"
    EXECUTING = "executing"
    BRIDGING = "bridging"
    RECONCILING = "reconciling"
    FAILED = "failed"
    DONE = "done"


@dataclass
class CycleRecord:
    id: str
    direction: str
    size_vchf: float
    state: CycleState = CycleState.IDLE
    simulation: CycleSimulation | None = None
    tx_hashes: list[str] = field(default_factory=list)
    error: str | None = None


class ArbExecutor:
    def __init__(
        self,
        chains: dict[str, ChainConfig],
        token: TokenConfig,
        bot_cfg: BotConfig | None = None,
    ) -> None:
        self.chains = chains
        self.token = token
        self.bot_cfg = bot_cfg or load_bot_config()
        self.celo = chains.get("celo")
        self.sol = chains.get("solana")
        self.vnx = chains.get("vnx")
        if not self.celo or not self.sol or not self.vnx:
            raise ValueError("celo, solana, and vnx chains required")

    def _may_reuse_on_chain_vchf(self) -> bool:
        """Platform-only treasury: always buy VCHF from hub stable, never reuse chain inventory."""
        return not self.bot_cfg.platform_vchf_only

    async def run_cycle(
        self,
        client: httpx.AsyncClient,
        direction: str,
        size_vchf: float,
        *,
        force_execute: bool = False,
    ) -> CycleRecord:
        cycle_id = str(uuid.uuid4())[:8]
        record = CycleRecord(id=cycle_id, direction=direction, size_vchf=size_vchf)
        record.state = CycleState.QUOTING

        sim = await simulate_direction(client, self.chains, self.token, self.bot_cfg, direction, size_vchf)
        record.simulation = sim
        if not force_execute:
            if not sim.profitable:
                record.state = CycleState.FAILED
                record.error = sim.error or "not profitable"
                save_cycle(record)
                return record

            if sim.net_profit_usd < self.bot_cfg.min_profit_usd:
                record.state = CycleState.FAILED
                record.error = f"profit ${sim.net_profit_usd:.2f} below min"
                save_cycle(record)
                return record
        elif sim.error and not sim.profitable:
            logger.warning("Force execute %s despite sim: %s", direction, sim.error)

        record.state = CycleState.EXECUTING
        log_cycle_step(cycle_id, "quote", {"net_profit_usd": sim.net_profit_usd, "dry_run": is_dry_run()})

        route = route_for_direction(direction)
        if route and route.needs_vnx_usdc and not self.bot_cfg.enable_vnx_arb_routes:
            record.state = CycleState.FAILED
            record.error = "celo↔vnx disabled — fund ETH USDC manually or enable ENABLE_VNX_ARB_ROUTES"
            save_cycle(record)
            return record
        if route and route.needs_cctp and not self.bot_cfg.enable_vnx_cctp_routes:
            record.state = CycleState.FAILED
            record.error = "SOL↔platform CCTP routes disabled (ENABLE_VNX_CCTP_ROUTES)"
            save_cycle(record)
            return record

        try:
            if direction == "celo_to_solana":
                await self._exec_celo_to_solana(client, record, sim)
            elif direction == "solana_to_celo":
                await self._exec_solana_to_celo(client, record, sim)
            elif route and route.buy_chain == "vnx":
                await self._exec_vnx_to_chain(client, record, sim, route.sell_chain)
            elif route and route.sell_chain == "vnx":
                await self._exec_chain_to_vnx(client, record, sim, route.buy_chain)
            else:
                record.state = CycleState.FAILED
                record.error = f"unsupported direction {direction}"
        except Exception as exc:
            record.state = CycleState.FAILED
            record.error = str(exc)[:300]
            logger.exception("Cycle %s failed", cycle_id)

        save_cycle(record)
        return record

    async def run_cctp_usdc_return_to_vnx(
        self,
        client: httpx.AsyncClient,
        usdc_amount: float,
        target_vchf: float,
        *,
        force_execute: bool = False,
    ) -> CycleRecord:
        """
        Closed-loop return: Sol USDC → CCTP → ETH → VNX USDC deposit → platform VCHF buy.
        """
        cycle_id = str(uuid.uuid4())[:8]
        record = CycleRecord(
            id=cycle_id,
            direction=CCTP_SOL_USDC_TO_VNX,
            size_vchf=target_vchf,
        )
        record.state = CycleState.QUOTING
        sim = await simulate_cctp_usdc_return_to_vnx(
            client, self.chains, self.token, self.bot_cfg, usdc_amount, target_vchf
        )
        record.simulation = sim
        if not force_execute:
            if not sim.profitable:
                record.state = CycleState.FAILED
                record.error = sim.error or "CCTP return not profitable"
                save_cycle(record)
                return record
            if sim.net_profit_usd < self.bot_cfg.min_profit_usd:
                record.state = CycleState.FAILED
                record.error = f"CCTP return profit ${sim.net_profit_usd:.2f} below min"
                save_cycle(record)
                return record
        elif sim.error and not sim.profitable:
            logger.warning("Force CCTP return despite sim: %s", sim.error)

        if not self.bot_cfg.enable_vnx_cctp_routes:
            record.state = CycleState.FAILED
            record.error = "CCTP routes disabled (ENABLE_VNX_CCTP_ROUTES)"
            save_cycle(record)
            return record

        try:
            record.state = CycleState.BRIDGING
            cctp = CircleCctpBridge()
            br = await cctp.bridge_usdc_sol_to_eth(client, usdc_amount)
            log_cycle_step(
                record.id,
                "cctp_sol_to_eth",
                {"amount": usdc_amount, "success": br.success, "dry_run": br.dry_run},
            )
            if not br.success:
                record.state = CycleState.FAILED
                record.error = br.error or "CCTP Sol→ETH failed"
                save_cycle(record)
                return record
            if br.source_tx:
                record.tx_hashes.append(br.source_tx)

            if br.source_tx and not br.dest_tx:
                from src.bridge.cctp_queue import CctpClaimQueue

                cfg_cctp = load_bridge_config()["cctp"]
                queue = CctpClaimQueue()
                queue.enqueue(
                    source_tx=br.source_tx,
                    source_domain=int(cfg_cctp["solana_domain"]),
                    dest_domain=int(cfg_cctp["ethereum_domain"]),
                    intent=CCTP_SOL_USDC_TO_VNX,
                )
                summary = await queue.run_until_empty(
                    client, interval_sec=15.0, max_rounds=40, discover_first=False
                )
                if summary.get("claimed", 0) < 1:
                    record.state = CycleState.FAILED
                    record.error = "CCTP claim to ETH timed out"
                    save_cycle(record)
                    return record

            eth_usdc = br.amount_usdc * 0.995 if br.amount_usdc else usdc_amount * 0.98
            record.state = CycleState.RECONCILING
            dep_err = check_usdc_deposit_amount("ETH", eth_usdc)
            if dep_err:
                logger.error("Aborting CCTP return ETH USDC→VNX deposit: %s", dep_err)
                record.state = CycleState.FAILED
                record.error = dep_err
                save_cycle(record)
                return record
            dep = await eth_usdc_to_vnx(client, eth_usdc, self.bot_cfg)
            log_cycle_step(record.id, "eth_usdc_to_vnx", dep)
            if not dep.get("success"):
                record.state = CycleState.FAILED
                record.error = dep.get("error") or "ETH USDC deposit to VNX failed"
                save_cycle(record)
                return record
            if dep.get("deposit_tx"):
                record.tx_hashes.append(dep["deposit_tx"])

            record.state = CycleState.EXECUTING
            buy = await platform_buy_vchf(
                self.bot_cfg,
                target_vchf,
                max_usdc=sim.stable_out_usd if sim.stable_out_usd > 0 else None,
            )
            if not buy.success:
                record.state = CycleState.FAILED
                record.error = buy.error or "platform VCHF buy after CCTP return failed"
                save_cycle(record)
                return record
            log_cycle_step(
                record.id,
                "vnx_buy_vchf",
                {
                    "quantity": buy.quantity,
                    "price": buy.price,
                    "ordid": buy.ordid,
                    "ordstatus": buy.ordstatus,
                    "dry_run": buy.dry_run,
                },
            )
            record.state = CycleState.DONE
        except Exception as exc:
            record.state = CycleState.FAILED
            record.error = str(exc)[:300]
            logger.exception("CCTP return %s failed", cycle_id)

        save_cycle(record)
        return record

    async def _exec_celo_to_solana(
        self, client: httpx.AsyncClient, record: CycleRecord, sim: CycleSimulation
    ) -> None:
        celo_exec = CeloExecutor(self.celo)
        sol_exec = SolanaExecutor(self.sol)
        celo_dec = token_decimals(self.token, "celo")
        sol_dec = token_decimals(self.token, "solana")
        target = record.size_vchf

        # Leg 1: USDT -> VCHF on Celo (skip if already holding enough)
        on_chain = float(to_human(celo_exec.balance_erc20(self.token.chains["celo"]), celo_dec))
        if self._may_reuse_on_chain_vchf() and on_chain >= target * 0.99:
            vchf_amt = min(on_chain, target)
            logger.info("Celo already has %.2f VCHF — skip buy", on_chain)
        else:
            vchf_amt = sim.token_mid if sim.token_mid > 0 else target
            usdt_in = from_human(sim.stable_in_usd, self.celo.hub_decimals)
            min_vchf = int(vchf_amt * 0.995 * 10**celo_dec)
            tx1 = celo_exec.swap_exact_input(
                self.celo.hub_token,
                self.token.chains["celo"],
                usdt_in,
                min_vchf,
            )
            if not tx1:
                record.state = CycleState.FAILED
                record.error = "celo buy VCHF failed"
                return
            record.tx_hashes.append(tx1)
            log_cycle_step(record.id, "celo_buy_vchf", {"tx": tx1})
            import time

            for _ in range(20):
                on_chain = float(to_human(celo_exec.balance_erc20(self.token.chains["celo"]), celo_dec))
                if on_chain >= target * 0.95:
                    break
                time.sleep(3)
            vchf_amt = min(on_chain, target)
            if vchf_amt < target * 0.9:
                record.state = CycleState.FAILED
                record.error = f"insufficient Celo VCHF after buy ({on_chain:.2f} < {target})"
                return
            logger.info("Celo VCHF after buy: %.4f (deposit %.4f)", on_chain, vchf_amt)

        # Bridge VCHF Celo -> Solana
        record.state = CycleState.BRIDGING
        bridge = VnxBridge(self.bot_cfg)

        async def deposit_builder(addr: str) -> str | None:
            amt = from_human(vchf_amt, celo_dec)
            return celo_exec.transfer_erc20(self.token.chains["celo"], addr, amt)

        import os

        br = await bridge.bridge_vchf(
            direction="celo_to_solana",
            quantity=vchf_amt,
            source_blockchain=os.getenv("VNX_CELO_BLOCKCHAIN", "CELO"),
            dest_blockchain=os.getenv("VNX_SOL_BLOCKCHAIN", "SOL"),
            dest_label=os.getenv("VNX_SOL_WITHDRAW_LABEL", "sol-hot"),
            deposit_tx_builder=deposit_builder,
        )
        if not br.success:
            record.state = CycleState.FAILED
            record.error = br.error or "bridge failed"
            return
        if br.deposit_tx:
            record.tx_hashes.append(br.deposit_tx)

        # Leg 2: VCHF -> USDC on Solana (wait for VNX withdraw to credit Sol)
        record.state = CycleState.EXECUTING
        from spl.token.instructions import get_associated_token_address
        from solders.pubkey import Pubkey

        vchf_ata = get_associated_token_address(
            sol_exec.keypair.pubkey(), Pubkey.from_string(self.token.chains["solana"])
        )
        deadline = time.time() + self.bot_cfg.vnx_bridge_timeout_sec
        needed = from_human(vchf_amt * 0.99, sol_dec)
        while time.time() < deadline:
            try:
                bal = sol_exec.token_account_balance(vchf_ata)
                if int(bal.value.amount) >= needed:
                    break
            except Exception:
                pass
            await asyncio.sleep(self.bot_cfg.vnx_bridge_poll_sec)
        else:
            from src.treasury.in_flight import InFlightLedger

            record.state = CycleState.FAILED
            record.error = (
                f"timeout waiting for VCHF on Sol after VNX withdraw — "
                f"funds may be pending at VNX ({InFlightLedger('VCHF').format_summary()})"
            )
            return

        vchf_in = from_human(vchf_amt, sol_dec)
        tx2 = await sol_exec.swap(
            client,
            self.token.chains["solana"],
            self.sol.hub_token,
            vchf_in,
            self.bot_cfg.slippage_bps,
        )
        if not tx2:
            record.state = CycleState.FAILED
            record.error = "solana sell VCHF failed"
            return
        record.tx_hashes.append(tx2)
        log_cycle_step(record.id, "sol_sell_vchf", {"tx": tx2})

        await self._reconcile_stable_usdt(client, record, "celo_to_solana", sim.stable_out_usd)
        record.state = CycleState.DONE

    async def _reconcile_stable_usdt(
        self,
        client: httpx.AsyncClient,
        record: CycleRecord,
        cycle_direction: str,
        usdt_amount: float,
    ) -> None:
        """Dry-run / probe Wormhole USDT rebalance (inverse of where stables landed)."""
        if usdt_amount <= 0:
            return
        record.state = CycleState.RECONCILING
        wh = WormholePortalBridge(self.celo)
        probe = max(1.0, usdt_amount * 0.01)
        sol_addr = os.getenv("SOLANA_PUBLIC_KEY", "")
        celo_addr = CeloExecutor(self.celo).address

        if cycle_direction == "celo_to_solana":
            # Stables on Sol (USDC); USDT rebalance path is Sol → Celo
            br = await wh.bridge_usdt_solana_to_celo(probe, celo_addr)
        elif cycle_direction == "solana_to_celo":
            # Stables on Celo (USDT); optional probe Celo → Sol
            br = await wh.bridge_usdt_celo_to_solana(probe, sol_addr) if sol_addr else None
        else:
            return

        if br is None:
            log_cycle_step(record.id, "wormhole_skip", {"reason": "no sol recipient"})
            return
        log_cycle_step(
            record.id,
            "wormhole_usdt",
            {"cycle": cycle_direction, "bridge": br.direction, "success": br.success, "dry_run": br.dry_run},
        )

    async def _exec_solana_to_celo(
        self, client: httpx.AsyncClient, record: CycleRecord, sim: CycleSimulation
    ) -> None:
        celo_exec = CeloExecutor(self.celo)
        sol_exec = SolanaExecutor(self.sol)
        celo_dec = token_decimals(self.token, "celo")
        sol_dec = token_decimals(self.token, "solana")
        target = record.size_vchf

        from spl.token.instructions import get_associated_token_address
        from solders.pubkey import Pubkey

        vchf_ata = get_associated_token_address(
            sol_exec.keypair.pubkey(), Pubkey.from_string(self.token.chains["solana"])
        )
        try:
            on_chain = sol_exec.token_balance_ui(vchf_ata)
        except Exception:
            on_chain = 0.0

        if self._may_reuse_on_chain_vchf() and on_chain >= target * 0.99:
            vchf_amt = min(on_chain, target)
            logger.info("Solana already has %.2f VCHF — skip buy, deposit %.2f", on_chain, vchf_amt)
        else:
            usdc_raw, _ = await usdc_raw_for_solana_buy(client, sim.stable_in_usd)
            usdc_in = usdc_raw if usdc_raw is not None else from_human(sim.stable_in_usd, self.sol.hub_decimals)
            tx1 = await sol_exec.swap(
                client,
                self.sol.hub_token,
                self.token.chains["solana"],
                usdc_in,
                self.bot_cfg.slippage_bps,
            )
            if not tx1:
                record.state = CycleState.FAILED
                record.error = "solana buy VCHF failed"
                return
            record.tx_hashes.append(tx1)

            import time

            for _ in range(30):
                try:
                    on_chain = sol_exec.token_balance_ui(vchf_ata)
                except Exception:
                    on_chain = 0.0
                if on_chain >= target * 0.95:
                    break
                time.sleep(SOL_BALANCE_POLL_SEC)
            vchf_amt = min(on_chain, target)
            if vchf_amt < target * 0.9:
                record.state = CycleState.FAILED
                record.error = f"insufficient VCHF after buy ({on_chain:.2f} < {target})"
                return
            logger.info("Solana VCHF after buy: %.4f (deposit %.4f)", on_chain, vchf_amt)

        record.state = CycleState.BRIDGING
        bridge = VnxBridge(self.bot_cfg)

        async def deposit_builder(addr: str) -> str | None:
            return sol_exec.transfer_spl(self.token.chains["solana"], addr, from_human(vchf_amt, sol_dec), sol_dec)

        import os

        br = await bridge.bridge_vchf(
            direction="solana_to_celo",
            quantity=vchf_amt,
            source_blockchain=os.getenv("VNX_SOL_BLOCKCHAIN", "SOL"),
            dest_blockchain=os.getenv("VNX_CELO_BLOCKCHAIN", "CELO"),
            dest_label=os.getenv("VNX_CELO_WITHDRAW_LABEL", "celo-hot"),
            deposit_tx_builder=deposit_builder,
        )
        if not br.success:
            record.state = CycleState.FAILED
            record.error = br.error or "bridge failed"
            return
        if br.deposit_tx:
            record.tx_hashes.append(br.deposit_tx)

        deadline = time.time() + self.bot_cfg.vnx_bridge_timeout_sec
        needed = from_human(vchf_amt * 0.99, celo_dec)
        while time.time() < deadline:
            if celo_exec.balance_erc20(self.token.chains["celo"]) >= needed:
                break
            await asyncio.sleep(self.bot_cfg.vnx_bridge_poll_sec)
        else:
            from src.treasury.in_flight import InFlightLedger

            record.state = CycleState.FAILED
            record.error = (
                f"timeout waiting for VCHF on Celo after VNX withdraw — "
                f"funds may be pending at VNX ({InFlightLedger('VCHF').format_summary()})"
            )
            return

        min_usdt = int(sim.stable_out_usd * 0.995 * 10**self.celo.hub_decimals)
        tx2 = celo_exec.swap_exact_input(
            self.token.chains["celo"],
            self.celo.hub_token,
            from_human(vchf_amt, celo_dec),
            min_usdt,
        )
        if not tx2:
            record.state = CycleState.FAILED
            record.error = "celo sell VCHF failed"
            return
        record.tx_hashes.append(tx2)
        await self._reconcile_stable_usdt(client, record, "solana_to_celo", sim.stable_out_usd)
        record.state = CycleState.DONE

    async def _exec_chain_to_vnx(
        self, client: httpx.AsyncClient, record: CycleRecord, sim: CycleSimulation, chain_key: str
    ) -> None:
        """Buy VCHF on chain → deposit to VNX → sell on platform (USDC)."""
        import os

        target = record.size_vchf
        vchf_amt = sim.token_mid if sim.token_mid > 0 else target
        log_cycle_step(record.id, "chain_buy_vchf", {"chain": chain_key, "dry_run": is_dry_run()})

        if chain_key == "celo":
            celo_exec = CeloExecutor(self.celo)
            dec = token_decimals(self.token, "celo")
            on_chain = float(to_human(celo_exec.balance_erc20(self.token.chains["celo"]), dec))
            if self._may_reuse_on_chain_vchf() and on_chain >= target * 0.99:
                vchf_amt = min(on_chain, target)
                logger.info("Celo already has %.2f VCHF — skip buy", on_chain)
            else:
                usdt_in = from_human(sim.stable_in_usd, self.celo.hub_decimals)
                tx = celo_exec.swap_exact_input(
                    self.celo.hub_token,
                    self.token.chains["celo"],
                    usdt_in,
                    int(vchf_amt * 0.995 * 10**dec),
                )
                if not tx:
                    record.state = CycleState.FAILED
                    record.error = "celo buy VCHF failed"
                    return
                record.tx_hashes.append(tx)

            async def deposit_builder(addr: str) -> str | None:
                return celo_exec.transfer_erc20(
                    self.token.chains["celo"], addr, from_human(vchf_amt, dec)
                )

            bc = os.getenv("VNX_CELO_BLOCKCHAIN", "CELO")
        else:
            sol_exec = SolanaExecutor(self.sol)
            dec = token_decimals(self.token, "solana")
            from spl.token.instructions import get_associated_token_address
            from solders.pubkey import Pubkey

            vchf_ata = get_associated_token_address(
                sol_exec.keypair.pubkey(), Pubkey.from_string(self.token.chains["solana"])
            )
            try:
                on_chain = sol_exec.token_balance_ui(vchf_ata)
            except Exception:
                on_chain = 0.0
            if self._may_reuse_on_chain_vchf() and on_chain >= target * 0.99:
                vchf_amt = min(on_chain, target)
                logger.info("Solana already has %.2f VCHF — skip buy", on_chain)
            else:
                usdc_in = from_human(sim.stable_in_usd, self.sol.hub_decimals)
                tx = await sol_exec.swap(
                    client,
                    self.sol.hub_token,
                    self.token.chains["solana"],
                    usdc_in,
                    self.bot_cfg.slippage_bps,
                )
                if not tx:
                    record.state = CycleState.FAILED
                    record.error = "solana buy VCHF failed"
                    return
                record.tx_hashes.append(tx)
                import time

                for _ in range(30):
                    try:
                        on_chain = sol_exec.token_balance_ui(vchf_ata)
                    except Exception:
                        on_chain = 0.0
                    if on_chain >= target * 0.95:
                        break
                    time.sleep(SOL_BALANCE_POLL_SEC)
                vchf_amt = min(on_chain, target)
                if vchf_amt < target * 0.9:
                    record.state = CycleState.FAILED
                    record.error = f"insufficient VCHF after buy ({on_chain:.2f} < {target})"
                    return
                logger.info("Solana VCHF balance after buy: %.4f (deposit %.4f)", on_chain, vchf_amt)

            async def deposit_builder(addr: str) -> str | None:
                return sol_exec.transfer_spl(
                    self.token.chains["solana"], addr, from_human(vchf_amt, dec), dec
                )

            bc = os.getenv("VNX_SOL_BLOCKCHAIN", "SOL")

        record.state = CycleState.BRIDGING
        bridge = VnxBridge(self.bot_cfg)
        br = await bridge.bridge_vchf(
            direction=record.direction,
            quantity=vchf_amt,
            source_blockchain=bc,
            dest_blockchain=bc,
            dest_label="platform",
            deposit_tx_builder=deposit_builder,
            deposit_only=True,
        )
        if not br.success:
            record.state = CycleState.FAILED
            record.error = br.error or "deposit to VNX failed"
            return
        if br.deposit_tx:
            record.tx_hashes.append(br.deposit_tx)

        sell = await platform_sell_vchf(self.bot_cfg, vchf_amt)
        if not sell.success:
            record.state = CycleState.FAILED
            record.error = sell.error or "VNX platform sell failed"
            return
        log_cycle_step(
            record.id,
            "vnx_sell_vchf",
            {
                "quantity": sell.quantity,
                "price": sell.price,
                "ordid": sell.ordid,
                "ordstatus": sell.ordstatus,
                "dry_run": sell.dry_run,
            },
        )

        route = route_for_direction(record.direction)
        if chain_key == "solana" and route and route.needs_cctp:
            await self._reconcile_cctp_platform(client, record, "solana_to_vnx", sim.stable_out_usd)

        record.state = CycleState.DONE

    async def _exec_vnx_to_chain(
        self, client: httpx.AsyncClient, record: CycleRecord, sim: CycleSimulation, chain_key: str
    ) -> None:
        """Buy VCHF on VNX platform → withdraw to chain → sell for stable."""
        import os

        target = record.size_vchf
        async with VnxClient() as vnx:
            bal = await vnx.account_balance()
            on_platform = vnx.vchf_balance(bal)
        if on_platform >= target * 0.99:
            vchf_amt = min(on_platform, target)
            logger.info("Platform already has %.2f VCHF — skip buy, withdraw %.2f", on_platform, vchf_amt)
        else:
            vchf_amt = sim.token_mid if sim.token_mid > 0 else target
            buy = await platform_buy_vchf(
                self.bot_cfg,
                vchf_amt,
                max_usdc=sim.stable_in_usd if sim.stable_in_usd > 0 else None,
            )
            if not buy.success:
                record.state = CycleState.FAILED
                record.error = buy.error or "VNX platform buy failed"
                return
            log_cycle_step(
                record.id,
                "vnx_buy_vchf",
                {
                    "quantity": buy.quantity,
                    "price": buy.price,
                    "ordid": buy.ordid,
                    "ordstatus": buy.ordstatus,
                    "dry_run": buy.dry_run,
                },
            )

        record.state = CycleState.BRIDGING
        dest_label = os.getenv(
            "VNX_CELO_WITHDRAW_LABEL" if chain_key == "celo" else "VNX_SOL_WITHDRAW_LABEL",
            f"{chain_key}-hot",
        )
        dest_bc = os.getenv(
            "VNX_CELO_BLOCKCHAIN" if chain_key == "celo" else "VNX_SOL_BLOCKCHAIN",
            "CELO" if chain_key == "celo" else "SOL",
        )

        bridge = VnxBridge(self.bot_cfg)
        br = await bridge.bridge_vchf(
            direction=record.direction,
            quantity=vchf_amt,
            source_blockchain=dest_bc,
            dest_blockchain=dest_bc,
            dest_label=dest_label,
            deposit_tx_builder=lambda _addr: None,
            withdraw_only=True,
        )
        if not br.success:
            record.state = CycleState.FAILED
            record.error = br.error or "withdraw from VNX failed"
            return
        if br.withdraw_txids:
            record.tx_hashes.extend(str(t) for t in br.withdraw_txids if t)
        vchf_amt = br.quantity

        record.state = CycleState.EXECUTING
        if chain_key == "celo":
            celo_exec = CeloExecutor(self.celo)
            dec = token_decimals(self.token, "celo")
            deadline = time.time() + self.bot_cfg.vnx_bridge_timeout_sec
            needed = from_human(vchf_amt * 0.99, dec)
            arrived = False
            while time.time() < deadline:
                if celo_exec.balance_erc20(self.token.chains["celo"]) >= needed:
                    arrived = True
                    break
                await asyncio.sleep(self.bot_cfg.vnx_bridge_poll_sec)
            if not arrived:
                from src.treasury.in_flight import InFlightLedger

                record.state = CycleState.FAILED
                record.error = (
                    f"timeout waiting for VCHF on Celo after VNX withdraw — "
                    f"funds may be pending at VNX ({InFlightLedger('VCHF').format_summary()})"
                )
                return
            min_usdt = int(sim.stable_out_usd * 0.995 * 10**self.celo.hub_decimals)
            tx = celo_exec.swap_exact_input(
                self.token.chains["celo"],
                self.celo.hub_token,
                from_human(vchf_amt, dec),
                min_usdt,
            )
        else:
            sol_exec = SolanaExecutor(self.sol)
            dec = token_decimals(self.token, "solana")
            from spl.token.instructions import get_associated_token_address
            from solders.pubkey import Pubkey

            vchf_mint = Pubkey.from_string(self.token.chains["solana"])
            vchf_ata = get_associated_token_address(sol_exec.keypair.pubkey(), vchf_mint)
            deadline = time.time() + self.bot_cfg.vnx_bridge_timeout_sec
            needed = from_human(vchf_amt * 0.99, dec)
            arrived = False
            while time.time() < deadline:
                try:
                    bal = sol_exec.token_account_balance(vchf_ata)
                    if int(bal.value.amount) >= needed:
                        arrived = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(self.bot_cfg.vnx_bridge_poll_sec)
            if not arrived:
                record.state = CycleState.FAILED
                from src.treasury.in_flight import InFlightLedger

                record.error = (
                    f"timeout waiting for VCHF on Sol after VNX withdraw — "
                    f"funds may be pending at VNX ({InFlightLedger('VCHF').format_summary()})"
                )
                return
            tx = await sol_exec.swap(
                client,
                self.token.chains["solana"],
                self.sol.hub_token,
                from_human(vchf_amt, dec),
                self.bot_cfg.slippage_bps,
            )

        if not tx:
            record.state = CycleState.FAILED
            record.error = f"{chain_key} sell VCHF failed"
            return
        record.tx_hashes.append(tx)
        route = route_for_direction(record.direction)
        if chain_key == "solana" and route and route.needs_cctp:
            # Full USDC return is handled by run_cctp_usdc_return_to_vnx in closed-loop mode.
            pass
        record.state = CycleState.DONE
        log_cycle_step(record.id, "chain_sell_vchf", {"chain": chain_key, "tx": tx})

    async def _reconcile_cctp_platform(
        self,
        client: httpx.AsyncClient,
        record: CycleRecord,
        cycle_direction: str,
        usdc_amount: float,
    ) -> None:
        """Optional CCTP USDC rebalance after VNX↔Sol routes (disable with CCTP_RECONCILE_USDC=0)."""
        if usdc_amount <= 0:
            return
        probe_usd = float(os.getenv("CCTP_RECONCILE_USDC", "0") or "0")
        if probe_usd <= 0:
            logger.info("CCTP reconcile skipped (CCTP_RECONCILE_USDC=0)")
            return
        record.state = CycleState.RECONCILING
        cctp = CircleCctpBridge()
        probe = probe_usd if probe_usd > 1 else max(10.0, usdc_amount * 0.01)
        # Inverse of where stables land: sol→vnx ends on ETH (refill Sol); vnx→sol ends on Sol (return to ETH)
        if cycle_direction == "solana_to_vnx":
            br = await cctp.bridge_usdc_eth_to_sol(client, probe)
        else:
            br = await cctp.bridge_usdc_sol_to_eth(client, probe)
        log_cycle_step(
            record.id,
            "cctp_usdc",
            {"cycle": cycle_direction, "bridge": br.direction, "success": br.success, "dry_run": br.dry_run},
        )
        if br.source_tx and not br.dest_tx:
            from src.bridge.cctp_queue import CctpClaimQueue

            cfg_cctp = load_bridge_config()["cctp"]
            if br.direction == "solana_to_ethereum_usdc":
                CctpClaimQueue().enqueue(
                    source_tx=br.source_tx,
                    source_domain=int(cfg_cctp["solana_domain"]),
                    dest_domain=int(cfg_cctp["ethereum_domain"]),
                    intent=cycle_direction,
                )
            elif br.direction == "ethereum_to_solana_usdc":
                CctpClaimQueue().enqueue(
                    source_tx=br.source_tx,
                    source_domain=int(cfg_cctp["ethereum_domain"]),
                    dest_domain=int(cfg_cctp["solana_domain"]),
                    intent=cycle_direction,
                )
