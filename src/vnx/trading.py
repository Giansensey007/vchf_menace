from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config_loader import BotConfig, is_dry_run
from src.vnx.client import VnxClient

logger = logging.getLogger(__name__)

VCHF_USDC = "VCHF/USDC"
VCHF_MIN_ORDER = 30.0
DEFAULT_QTY_DECIMALS = 5
DEFAULT_PRICE_DECIMALS = 6
# From VNX tradingPairs — avoid calling tradingPairs immediately before quotes (rate-limit quirk)
VCHF_USDC_QTY_DECIMALS = 5


@dataclass
class PlatformOrderResult:
    success: bool
    side: str
    quantity: float
    price: float
    clordid: str
    ordid: int | None = None
    ordstatus: str = ""
    bought: float = 0.0
    bought_currency: str = ""
    sold: float = 0.0
    sold_currency: str = ""
    fee: float = 0.0
    fee_currency: str = ""
    dry_run: bool = False
    error: str | None = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_clordid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _round_down(value: float, decimals: int) -> float:
    factor = 10**decimals
    return int(value * factor) / factor


def _quote_side_prices(quotes: dict, pair: str) -> tuple[float, float, float, float]:
    row = quotes.get(pair) or {}
    bid = row.get("b") or []
    ask = row.get("a") or []
    bid_price = float(bid[0]) if len(bid) >= 1 else 0.0
    bid_liq = float(bid[1]) if len(bid) >= 2 else 0.0
    ask_price = float(ask[0]) if len(ask) >= 1 else 0.0
    ask_liq = float(ask[1]) if len(ask) >= 2 else 0.0
    return bid_price, bid_liq, ask_price, ask_liq


def _limit_price(side: str, bid: float, ask: float, slippage_bps: int) -> float:
    slip = slippage_bps / 10_000.0
    if side == "Sell":
        return _round_down(bid * (1.0 - slip), DEFAULT_PRICE_DECIMALS)
    return _round_down(ask * (1.0 + slip), DEFAULT_PRICE_DECIMALS)


async def _load_bid_ask(vnx: VnxClient) -> tuple[dict, str | None]:
    data = await vnx.get_quotes()
    quotes = data.get("quotes") or []
    if quotes and quotes[0].get("pair") and not quotes[0].get("symbol"):
        return {}, "VNX quotes unavailable (no bid/ask) — retry in a few seconds"
    by_symbol = {q["symbol"]: q for q in quotes if q.get("symbol")}
    if VCHF_USDC not in by_symbol:
        return {}, f"no quote for {VCHF_USDC}"
    return by_symbol, None


async def _load_bid_ask_with_retry(vnx: VnxClient) -> tuple[dict, str | None]:
    import asyncio
    import os

    max_attempts = int(os.getenv("VNX_QUOTE_RETRY_MAX", "5"))
    for attempt in range(max_attempts):
        by_symbol, err = await _load_bid_ask(vnx)
        if not err:
            return by_symbol, None
        if attempt + 1 >= max_attempts:
            return by_symbol, err
        wait = 8.0 if attempt == 0 else 4.0
        logger.debug("VNX quote retry in %.0fs: %s", wait, err)
        await asyncio.sleep(wait)
    return {}, "VNX quotes unavailable after retries"


async def _pair_qty_decimals(vnx: VnxClient) -> int:
    return VCHF_USDC_QTY_DECIMALS


async def _submit_fok(
    vnx: VnxClient,
    *,
    side: str,
    quantity: float,
    price: float,
    clordid_prefix: str,
) -> PlatformOrderResult:
    qty = quantity
    payload = {
        "timestamp": _utc_timestamp(),
        "clordid": _new_clordid(clordid_prefix),
        "symbol": VCHF_USDC,
        "side": side,
        "ordtype": "Limit",
        "timeinforce": "FOK",
        "orderqty": qty,
        "price": price,
    }

    if is_dry_run():
        logger.info(
            "[DRY_RUN] VNX %s %.4f VCHF @ %.6f USDC (FOK) clordid=%s",
            side,
            qty,
            price,
            payload["clordid"],
        )
        return PlatformOrderResult(
            success=True,
            side=side,
            quantity=qty,
            price=price,
            clordid=payload["clordid"],
            ordid=0,
            ordstatus="Filled",
            bought=qty if side == "Buy" else price * qty,
            bought_currency="VCHF" if side == "Buy" else "USDC",
            sold=price * qty if side == "Buy" else qty,
            sold_currency="USDC" if side == "Buy" else "VCHF",
            dry_run=True,
        )

    try:
        data = await vnx.add_order(payload)
    except Exception as exc:
        return PlatformOrderResult(
            success=False,
            side=side,
            quantity=qty,
            price=price,
            clordid=payload["clordid"],
            error=str(exc)[:300],
        )

    if data.get("result") != "success":
        err = data.get("error") or {}
        msg = err.get("message") or err.get("code") or "addOrder failed"
        return PlatformOrderResult(
            success=False,
            side=side,
            quantity=qty,
            price=price,
            clordid=payload["clordid"],
            error=str(msg),
        )

    order = data.get("order") or {}
    status = str(order.get("ordstatus") or "")
    filled = status == "Filled"
    return PlatformOrderResult(
        success=filled,
        side=side,
        quantity=qty,
        price=price,
        clordid=str(order.get("clordid") or payload["clordid"]),
        ordid=order.get("ordid"),
        ordstatus=status,
        bought=float(order.get("bought") or 0),
        bought_currency=str(order.get("bought_currency") or ""),
        sold=float(order.get("sold") or 0),
        sold_currency=str(order.get("sold_currency") or ""),
        fee=float(order.get("fee") or 0),
        fee_currency=str(order.get("fee_currency") or ""),
        error=None if filled else f"order status {status}",
    )


async def _submit_fok_with_retry(
    vnx: VnxClient,
    *,
    side: str,
    quantity: float,
    price: float,
    clordid_prefix: str,
    bot_cfg: BotConfig,
) -> PlatformOrderResult:
    import asyncio
    import os

    max_attempts = int(os.getenv("VNX_ORDER_RETRY_MAX", "3"))
    last: PlatformOrderResult | None = None
    for attempt in range(max_attempts):
        result = await _submit_fok(
            vnx, side=side, quantity=quantity, price=price, clordid_prefix=clordid_prefix
        )
        if result.success:
            return result
        last = result
        err = (result.error or "").lower()
        retryable = any(
            k in err
            for k in (
                "retry",
                "unavailable",
                "invalid_request_limit",
                "rejected",
                "expired",
                "partial",
                "status",
            )
        )
        if not retryable or attempt + 1 >= max_attempts:
            return result
        by_symbol, q_err = await _load_bid_ask_with_retry(vnx)
        if q_err:
            await asyncio.sleep(4.0)
            continue
        bid, _, ask, _ = _quote_side_prices(by_symbol, VCHF_USDC)
        price = _limit_price(side, bid, ask, bot_cfg.slippage_bps)
        logger.warning("VNX order retry %s/%s after: %s", attempt + 2, max_attempts, result.error)
        await asyncio.sleep(2.0 + attempt)
    return last or PlatformOrderResult(
        success=False, side=side, quantity=quantity, price=price, clordid="", error="order failed"
    )


async def platform_sell_vchf(
    bot_cfg: BotConfig,
    quantity: float,
    *,
    vnx: VnxClient | None = None,
) -> PlatformOrderResult:
    """Sell VCHF on VNX platform for USDC (FOK limit at bid)."""
    if quantity < VCHF_MIN_ORDER:
        return PlatformOrderResult(
            success=False,
            side="Sell",
            quantity=quantity,
            price=0.0,
            clordid="",
            error=f"below VNX min order ({VCHF_MIN_ORDER} VCHF)",
        )

    async def _run(client: VnxClient) -> PlatformOrderResult:
        qty_decimals = await _pair_qty_decimals(client)
        qty = _round_down(quantity, qty_decimals)
        if qty < VCHF_MIN_ORDER:
            return PlatformOrderResult(
                success=False,
                side="Sell",
                quantity=quantity,
                price=0.0,
                clordid="",
                error=f"rounded qty {qty} below VNX min",
            )

        by_symbol, q_err = await _load_bid_ask_with_retry(client)
        if q_err:
            return PlatformOrderResult(
                success=False, side="Sell", quantity=qty, price=0.0, clordid="", error=q_err
            )
        bid, bid_liq, _, _ = _quote_side_prices(by_symbol, VCHF_USDC)
        if bid <= 0 or bid_liq < qty * 0.999:
            return PlatformOrderResult(
                success=False,
                side="Sell",
                quantity=qty,
                price=bid,
                clordid="",
                error=f"insufficient VNX bid ({bid_liq:.2f} < {qty:.2f} VCHF)",
            )

        price = _limit_price("Sell", bid, 0.0, bot_cfg.slippage_bps)
        bal = client.vchf_balance(await client.account_balance())
        if not is_dry_run() and bal < qty * 0.99:
            return PlatformOrderResult(
                success=False,
                side="Sell",
                quantity=qty,
                price=price,
                clordid="",
                error=f"insufficient platform VCHF ({bal:.2f} < {qty:.2f})",
            )
        return await _submit_fok_with_retry(
            client, side="Sell", quantity=qty, price=price, clordid_prefix="sell", bot_cfg=bot_cfg
        )

    if vnx is not None:
        return await _run(vnx)
    async with VnxClient() as client:
        return await _run(client)


async def platform_buy_vchf(
    bot_cfg: BotConfig,
    quantity: float,
    *,
    max_usdc: float | None = None,
    vnx: VnxClient | None = None,
) -> PlatformOrderResult:
    """Buy VCHF on VNX platform with USDC (FOK limit at ask)."""
    if quantity < VCHF_MIN_ORDER:
        return PlatformOrderResult(
            success=False,
            side="Buy",
            quantity=quantity,
            price=0.0,
            clordid="",
            error=f"below VNX min order ({VCHF_MIN_ORDER} VCHF)",
        )

    async def _run(client: VnxClient) -> PlatformOrderResult:
        qty_decimals = await _pair_qty_decimals(client)
        qty = _round_down(quantity, qty_decimals)
        if qty < VCHF_MIN_ORDER:
            return PlatformOrderResult(
                success=False,
                side="Buy",
                quantity=quantity,
                price=0.0,
                clordid="",
                error=f"rounded qty {qty} below VNX min",
            )

        by_symbol, q_err = await _load_bid_ask_with_retry(client)
        if q_err:
            return PlatformOrderResult(
                success=False, side="Buy", quantity=qty, price=0.0, clordid="", error=q_err
            )
        _, _, ask, ask_liq = _quote_side_prices(by_symbol, VCHF_USDC)
        if ask <= 0 or ask_liq < qty * 0.999:
            return PlatformOrderResult(
                success=False,
                side="Buy",
                quantity=qty,
                price=ask,
                clordid="",
                error=f"insufficient VNX ask ({ask_liq:.2f} < {qty:.2f} VCHF)",
            )

        price = _limit_price("Buy", 0.0, ask, bot_cfg.slippage_bps)
        usdc_needed = qty * price
        if max_usdc is not None and usdc_needed > max_usdc * 1.01:
            return PlatformOrderResult(
                success=False,
                side="Buy",
                quantity=qty,
                price=price,
                clordid="",
                error=f"USDC cost {usdc_needed:.2f} exceeds budget {max_usdc:.2f}",
            )

        usdc_bal = client.usdc_balance(await client.account_balance())
        if not is_dry_run() and usdc_bal < usdc_needed * 0.99:
            return PlatformOrderResult(
                success=False,
                side="Buy",
                quantity=qty,
                price=price,
                clordid="",
                error=f"insufficient platform USDC ({usdc_bal:.2f} < {usdc_needed:.2f})",
            )
        return await _submit_fok_with_retry(
            client, side="Buy", quantity=qty, price=price, clordid_prefix="buy", bot_cfg=bot_cfg
        )

    if vnx is not None:
        return await _run(vnx)
    async with VnxClient() as client:
        return await _run(client)
