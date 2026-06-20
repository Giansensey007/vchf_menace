from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx

from src.config_loader import BotConfig, is_dry_run
from src.vnx.client import VnxClient
from src.vnx.collision import (
    collision_backoff_sec,
    collision_retry_max,
    is_vnx_collision_error,
    vnx_error_message,
)
from src.vnx.deposits import validate_eth_usdc_vnx_deposit
from src.vnx.constants import VNX_ETH_DEPOSIT_ASSET, check_vnx_eth_deposit_asset

logger = logging.getLogger(__name__)

USDC_QTY_DECIMALS = 2


def _round_usdc(qty: float) -> float:
    from decimal import Decimal, ROUND_DOWN

    return float(Decimal(str(qty)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


@dataclass
class UsdcBridgeResult:
    direction: str
    quantity: float
    deposit_address: str
    withdraw_destination: str
    deposit_tx: str | None
    withdraw_txids: list | None
    dry_run: bool
    success: bool
    error: str | None = None


class VnxUsdcBridge:
    """USDC on Ethereum ↔ VNX platform (VNX settles USDC via ETH only)."""

    def __init__(self, bot_cfg: BotConfig) -> None:
        self.cfg = bot_cfg
        self.eth_blockchain = os.getenv("VNX_ETH_BLOCKCHAIN", "ETH").strip().upper()
        eth_label = os.getenv("VNX_ETH_WITHDRAW_LABEL", "arb_explorer_mainnet_USDC").strip()
        self.withdraw_label = eth_label
        if eth_label == os.getenv("VNX_BASE_WITHDRAW_LABEL", "").strip():
            logger.warning(
                "VNX_ETH_WITHDRAW_LABEL matches BASE label — USDC withdraw requires ETH whitelist label "
                "(e.g. arb_explorer_mainnet_USDC)"
            )

    async def deposit_usdc(
        self,
        quantity: float,
        *,
        deposit_tx_builder,
        direction: str = "eth_to_vnx",
    ) -> UsdcBridgeResult:
        """On-chain USDC transfer to VNX deposit address + poll platform credit."""
        quantity = _round_usdc(quantity)
        if quantity <= 0:
            return UsdcBridgeResult(direction, quantity, "", "", None, None, is_dry_run(), False, "zero quantity")

        dep_err = validate_eth_usdc_vnx_deposit(quantity, blockchain=self.eth_blockchain)
        if dep_err:
            logger.error("Aborting USDC deposit to VNX (%s): %s", self.eth_blockchain, dep_err)
            return UsdcBridgeResult(direction, quantity, "", "", None, None, is_dry_run(), False, dep_err)

        asset_guard = check_vnx_eth_deposit_asset(VNX_ETH_DEPOSIT_ASSET, self.eth_blockchain)
        if asset_guard:
            logger.error("VNX ETH deposit asset guard: %s", asset_guard)
            return UsdcBridgeResult(direction, quantity, "", "", None, None, is_dry_run(), False, asset_guard)

        if is_dry_run():
            logger.info("[DRY_RUN] USDC deposit %.2f to VNX (%s)", quantity, self.eth_blockchain)
            return UsdcBridgeResult(
                direction, quantity, "dry-run-deposit-addr", "platform", "dry-run-usdc-deposit", None, True, True
            )

        async with VnxClient() as vnx:
            dep = await vnx.deposit_address("USDC", self.eth_blockchain)
            deposit_address = dep.get("address") or ""
            if not deposit_address:
                return UsdcBridgeResult(
                    direction, quantity, "", "", None, None, is_dry_run(), False, "no USDC deposit address"
                )

            logger.info(
                "USDC bridge %s: deposit %.2f USDC to %s (%s)",
                direction,
                quantity,
                deposit_address[:16],
                self.eth_blockchain,
            )

            bal0 = await vnx.account_balance_resilient()
            err0 = vnx_error_message(bal0)
            if err0:
                return UsdcBridgeResult(
                    direction, quantity, "", "", None, None, False, False, f"VNX balance unavailable: {err0}"
                )
            balance_before = vnx.usdc_balance(bal0)
            deposit_tx = await deposit_tx_builder(deposit_address)
            if not deposit_tx:
                return UsdcBridgeResult(
                    direction, quantity, deposit_address, "platform", None, None, False, False, "deposit tx failed"
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
                    logger.warning("VNX USDC poll failed (%s): %s", poll_errors, exc)
                    if poll_errors >= 3:
                        await asyncio.sleep(min(30, self.cfg.vnx_bridge_poll_sec))
                    continue
                err_msg = vnx_error_message(bal_resp)
                if err_msg:
                    poll_errors += 1
                    if is_vnx_collision_error(err_msg):
                        logger.warning("VNX USDC poll contention (%s): %s", poll_errors, err_msg)
                        await asyncio.sleep(collision_backoff_sec(min(poll_errors - 1, 2)))
                    else:
                        logger.warning("VNX USDC poll error: %s", err_msg)
                    continue
                poll_errors = 0
                credited = vnx.usdc_balance(bal_resp)
                if credited >= balance_before + quantity * 0.99:
                    logger.info("USDC credited on platform: %.2f (was %.2f)", credited, balance_before)
                    return UsdcBridgeResult(
                        direction, quantity, deposit_address, "platform", deposit_tx, None, False, True
                    )

            return UsdcBridgeResult(
                direction,
                quantity,
                deposit_address,
                "platform",
                deposit_tx,
                None,
                False,
                False,
                f"USDC deposit not credited in time (before={balance_before:.2f}, after={credited:.2f})",
            )

    async def withdraw_usdc(
        self,
        quantity: float,
        *,
        direction: str = "vnx_to_eth",
        dest_label: str | None = None,
    ) -> UsdcBridgeResult:
        """Withdraw platform USDC to whitelisted ETH address label."""
        quantity = _round_usdc(quantity)
        label = dest_label or self.withdraw_label
        if quantity <= 0:
            return UsdcBridgeResult(direction, quantity, "", label, None, None, is_dry_run(), False, "zero quantity")

        async with VnxClient() as vnx:
            bal = await vnx.account_balance_resilient()
            err = vnx_error_message(bal)
            if err:
                return UsdcBridgeResult(
                    direction, quantity, "", label, None, None, False, False, f"VNX balance unavailable: {err}"
                )
            balance = vnx.usdc_balance(bal)
            withdraw_qty = _round_usdc(min(quantity, balance))
            if withdraw_qty < quantity * 0.95:
                return UsdcBridgeResult(
                    direction,
                    quantity,
                    "",
                    label,
                    None,
                    None,
                    False,
                    False,
                    f"insufficient platform USDC ({balance:.2f} < {quantity:.2f})",
                )

            logger.info("USDC bridge %s: withdraw %.2f USDC → %s", direction, withdraw_qty, label)
            if is_dry_run():
                return UsdcBridgeResult(
                    direction, withdraw_qty, "", label, None, ["dry-run-usdc-withdraw"], True, True
                )

            last_err: str | None = None
            for attempt in range(collision_retry_max()):
                try:
                    wd = await vnx.withdraw(
                        "USDC", withdraw_qty, label, blockchain=self.eth_blockchain
                    )
                    if wd.get("result") == "error":
                        err = wd.get("error") or {}
                        last_err = str(err.get("message") or err.get("code") or "withdraw failed")
                        if is_vnx_collision_error(last_err) and attempt + 1 < collision_retry_max():
                            logger.warning(
                                "VNX USDC withdraw contention (attempt %s/%s): %s",
                                attempt + 1,
                                collision_retry_max(),
                                last_err,
                            )
                            await asyncio.sleep(collision_backoff_sec(attempt))
                            continue
                        return UsdcBridgeResult(
                            direction, withdraw_qty, "", label, None, None, False, False, last_err
                        )
                    txids = wd.get("txids") or []
                    return UsdcBridgeResult(direction, withdraw_qty, "", label, None, txids, False, True)
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:300] if exc.response else str(exc)
                    last_err = body
                    if is_vnx_collision_error(body) and attempt + 1 < collision_retry_max():
                        logger.warning(
                            "VNX USDC withdraw contention (attempt %s/%s): %s",
                            attempt + 1,
                            collision_retry_max(),
                            body[:200],
                        )
                        await asyncio.sleep(collision_backoff_sec(attempt))
                        continue
                    logger.warning("VNX USDC withdraw failed: %s", body)
                    return UsdcBridgeResult(
                        direction, withdraw_qty, "", label, None, None, False, False, body
                    )
            return UsdcBridgeResult(
                direction, withdraw_qty, "", label, None, None, False, False, last_err or "withdraw failed"
            )
