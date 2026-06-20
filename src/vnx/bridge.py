from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from src.config_loader import BotConfig, is_dry_run, load_chains, load_tokens
from src.treasury.in_flight import InFlightLedger, read_on_chain_token_balances
from src.vnx.client import VnxClient
from src.vnx.collision import (
    collision_backoff_sec,
    collision_retry_max,
    is_vnx_collision_error,
    vnx_error_message,
)
from src.vnx.trading import _round_down, VCHF_USDC_QTY_DECIMALS
from src.vnx.deposits import check_deposit_amount

logger = logging.getLogger(__name__)

# VCHF network fee buffer for platform withdraw (qty + fee must be <= balance)
VCHF_WITHDRAW_FEE_BUFFER = 1.35


async def _withdraw_with_collision_retry(
    vnx: VnxClient,
    asset: str,
    quantity: float,
    dest_label: str,
    *,
    blockchain: str | None = None,
) -> tuple[dict | None, str | None]:
    """Withdraw with backoff on shared-account contention; never raises."""
    last_err: str | None = None
    for attempt in range(collision_retry_max()):
        try:
            if blockchain:
                wd = await vnx.withdraw(asset, quantity, dest_label, blockchain=blockchain)
            else:
                wd = await vnx.withdraw(asset, quantity, dest_label)
            if wd.get("result") == "error":
                err = wd.get("error") or {}
                last_err = str(err.get("message") or err.get("code") or "withdraw failed")
                if is_vnx_collision_error(last_err) and attempt + 1 < collision_retry_max():
                    logger.warning(
                        "VNX withdraw contention (attempt %s/%s): %s",
                        attempt + 1,
                        collision_retry_max(),
                        last_err,
                    )
                    await asyncio.sleep(collision_backoff_sec(attempt))
                    continue
                return None, last_err
            return wd, None
        except Exception as exc:
            last_err = str(exc)[:300]
            if is_vnx_collision_error(last_err) and attempt + 1 < collision_retry_max():
                logger.warning(
                    "VNX withdraw contention (attempt %s/%s): %s",
                    attempt + 1,
                    collision_retry_max(),
                    last_err,
                )
                await asyncio.sleep(collision_backoff_sec(attempt))
                continue
            logger.warning("VNX withdraw failed: %s", last_err)
            return None, last_err
    return None, last_err or "withdraw failed after retries"


@dataclass
class BridgeResult:
    direction: str
    quantity: float
    deposit_address: str
    withdraw_destination: str
    deposit_tx: str | None
    withdraw_txids: list | None
    dry_run: bool
    success: bool
    error: str | None = None


class VnxBridge:
    """Orchestrate VCHF rebalancing via VNX Platform deposit + withdraw."""

    def __init__(self, bot_cfg: BotConfig) -> None:
        self.cfg = bot_cfg
        self._ledger = InFlightLedger("VCHF")

    async def bridge_vchf(
        self,
        *,
        direction: str,
        quantity: float,
        source_blockchain: str,
        dest_blockchain: str,
        dest_label: str,
        deposit_tx_builder,
        withdraw_only: bool = False,
        deposit_only: bool = False,
    ) -> BridgeResult:
        """
        direction: e.g. base_to_solana
        deposit_tx_builder: async callable(deposit_address) -> tx_hash | None
        withdraw_only: withdraw platform VCHF to whitelisted label (vnx_to_* routes)
        deposit_only: on-chain deposit + wait for platform credit; no withdraw (chain_to_vnx)
        """
        if withdraw_only and deposit_only:
            return BridgeResult(
                direction, quantity, "", dest_label, None, None, is_dry_run(), False,
                "withdraw_only and deposit_only are mutually exclusive",
            )
        if quantity <= 0:
            return BridgeResult(
                direction, quantity, "", dest_label, None, None, is_dry_run(), False, "zero quantity"
            )

        if not withdraw_only:
            dep_err = check_deposit_amount(source_blockchain, quantity)
            if dep_err:
                logger.error("Bridge %s blocked: %s", direction, dep_err)
                return BridgeResult(
                    direction, quantity, "", dest_label, None, None, is_dry_run(), False, dep_err
                )

        async with VnxClient() as vnx:
            if withdraw_only:
                logger.info(
                    "Bridge %s: withdraw %.4f VCHF from platform → %s",
                    direction,
                    quantity,
                    dest_label,
                )
                if is_dry_run():
                    logger.info("[DRY_RUN] skip platform withdraw")
                    return BridgeResult(
                        direction, quantity, "", dest_label, None, ["dry-run-withdraw"], True, True
                    )

                balance = vnx.vchf_balance(await vnx.account_balance_resilient())
                withdraw_qty = _round_down(
                    min(quantity, balance - VCHF_WITHDRAW_FEE_BUFFER), VCHF_USDC_QTY_DECIMALS
                )
                if withdraw_qty < quantity * 0.95:
                    return BridgeResult(
                        direction,
                        quantity,
                        "",
                        dest_label,
                        None,
                        None,
                        False,
                        False,
                        f"insufficient platform VCHF for withdraw+fee "
                        f"(balance={balance:.2f}, need~{quantity + VCHF_WITHDRAW_FEE_BUFFER:.2f})",
                    )
                if withdraw_qty <= 0:
                    return BridgeResult(
                        direction, quantity, "", dest_label, None, None, False, False, "zero withdraw qty"
                    )

                pending = self._ledger.pending_for_blockchain(dest_blockchain)
                if pending:
                    logger.info(
                        "Bridge %s: skip duplicate withdraw — %.2f VCHF already pending to %s",
                        direction,
                        sum(p.quantity for p in pending),
                        dest_blockchain,
                    )
                    return BridgeResult(
                        direction,
                        sum(p.quantity for p in pending),
                        "",
                        dest_label,
                        None,
                        [t for p in pending for t in p.txids],
                        False,
                        True,
                    )

                wd, wd_err = await _withdraw_with_collision_retry(
                    vnx, "VCHF", withdraw_qty, dest_label
                )
                if wd_err:
                    return BridgeResult(
                        direction,
                        quantity,
                        "",
                        dest_label,
                        None,
                        None,
                        False,
                        False,
                        wd_err,
                    )
                txids = (wd or {}).get("txids")
                chains = load_chains()
                token = load_tokens()["VCHF"]
                base_base, celo_base, sol_base = read_on_chain_token_balances(chains, token)
                self._ledger.log_vnx_withdraw(
                    withdraw_qty,
                    dest_blockchain,
                    dest_label,
                    direction,
                    txids,
                    baseline_base_token=base_base,
                    baseline_celo_token=celo_base,
                    baseline_sol_token=sol_base,
                    baseline_platform_token=balance,
                )
                return BridgeResult(
                    direction, withdraw_qty, "", dest_label, None, txids, False, True
                )

            dep = await vnx.deposit_address("VCHF", source_blockchain)
            deposit_address = dep.get("address") or ""
            if not deposit_address:
                return BridgeResult(
                    direction, quantity, "", dest_label, None, None, is_dry_run(), False, "no deposit address"
                )

            logger.info(
                "Bridge %s: deposit %.4f VCHF to %s (%s)",
                direction,
                quantity,
                deposit_address[:16],
                source_blockchain,
            )

            if is_dry_run():
                logger.info("[DRY_RUN] skip on-chain deposit%s", " (deposit-only)" if deposit_only else " and withdraw")
                return BridgeResult(
                    direction, quantity, deposit_address, dest_label, "dry-run", None, True, True
                )

            balance_before = vnx.vchf_balance(await vnx.account_balance_resilient())
            deposit_tx = await deposit_tx_builder(deposit_address)
            if not deposit_tx:
                return BridgeResult(
                    direction,
                    quantity,
                    deposit_address,
                    dest_label,
                    None,
                    None,
                    False,
                    False,
                    "deposit tx failed",
                )

            self._ledger.log_vnx_deposit(
                quantity,
                source_blockchain,
                direction,
                deposit_tx,
                baseline_platform_token=balance_before,
            )

            deadline = time.time() + self.cfg.vnx_bridge_timeout_sec
            credited = balance_before
            poll_errors = 0
            while time.time() < deadline:
                await asyncio.sleep(self.cfg.vnx_bridge_poll_sec)
                try:
                    bal_resp = await vnx.account_balance()
                except Exception as exc:
                    poll_errors += 1
                    logger.warning("VNX balance poll failed (%s): %s", poll_errors, exc)
                    if poll_errors >= 3:
                        await asyncio.sleep(min(30, self.cfg.vnx_bridge_poll_sec))
                    continue
                err_msg = vnx_error_message(bal_resp)
                if err_msg:
                    poll_errors += 1
                    if is_vnx_collision_error(err_msg):
                        logger.warning("VNX balance poll contention (%s): %s", poll_errors, err_msg)
                        await asyncio.sleep(collision_backoff_sec(min(poll_errors - 1, 2)))
                    else:
                        logger.warning("VNX balance poll error: %s", err_msg)
                    continue
                poll_errors = 0
                credited = vnx.vchf_balance(bal_resp)
                if credited >= balance_before + quantity * 0.99:
                    break
            else:
                return BridgeResult(
                    direction,
                    quantity,
                    deposit_address,
                    dest_label,
                    deposit_tx,
                    None,
                    False,
                    False,
                    "deposit not credited in time "
                    "(check VNX min deposit per chain — BASE requires cumulative ≥5 VCHF)",
                )

            if deposit_only:
                logger.info(
                    "Bridge %s: %.4f VCHF credited on platform (deposit-only)",
                    direction,
                    quantity,
                )
                return BridgeResult(
                    direction, quantity, deposit_address, dest_label, deposit_tx, None, False, True
                )

            balance = vnx.vchf_balance(await vnx.account_balance_resilient())
            withdraw_qty = _round_down(
                min(quantity, balance - VCHF_WITHDRAW_FEE_BUFFER), VCHF_USDC_QTY_DECIMALS
            )
            if withdraw_qty < quantity * 0.95:
                return BridgeResult(
                    direction,
                    quantity,
                    deposit_address,
                    dest_label,
                    deposit_tx,
                    None,
                    False,
                    False,
                    f"insufficient platform VCHF for withdraw+fee "
                    f"(balance={balance:.2f}, need~{quantity + VCHF_WITHDRAW_FEE_BUFFER:.2f})",
                )
            if withdraw_qty <= 0:
                return BridgeResult(
                    direction, quantity, deposit_address, dest_label, deposit_tx, None, False, False, "zero withdraw qty"
                )

            pending = self._ledger.pending_for_blockchain(dest_blockchain)
            if pending:
                logger.info(
                    "Bridge %s: skip duplicate withdraw after deposit — pending to %s",
                    direction,
                    dest_blockchain,
                )
                txids = [t for p in pending for t in p.txids]
            else:
                wd, wd_err = await _withdraw_with_collision_retry(
                    vnx, "VCHF", withdraw_qty, dest_label
                )
                if wd_err:
                    return BridgeResult(
                        direction,
                        quantity,
                        deposit_address,
                        dest_label,
                        deposit_tx,
                        None,
                        False,
                        False,
                        wd_err,
                    )
                txids = (wd or {}).get("txids")
                chains = load_chains()
                token = load_tokens()["VCHF"]
                base_base, celo_base, sol_base = read_on_chain_token_balances(chains, token)
                self._ledger.log_vnx_withdraw(
                    withdraw_qty,
                    dest_blockchain,
                    dest_label,
                    direction,
                    txids,
                    baseline_base_token=base_base,
                    baseline_celo_token=celo_base,
                    baseline_sol_token=sol_base,
                    baseline_platform_token=balance,
                )
            return BridgeResult(
                direction,
                withdraw_qty,
                deposit_address,
                dest_label,
                deposit_tx,
                txids,
                False,
                True,
            )
