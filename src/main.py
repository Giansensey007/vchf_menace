from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import is_dry_run, load_bot_config, load_chains, load_tokens
from src.db import init_db
from src.execution.executor import ArbExecutor
from src.quotes.http_client import build_client
from src.scanner.arb import ArbScanner
from src.treasury.manager import TreasuryManager
from src.treasury.loops import origin_for_direction
from src.vnx.auth import ensure_public_key_env
from src.vnx.collision import is_vnx_collision_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("vchf_menace")


async def run_once() -> None:
    bot_cfg = load_bot_config()
    scanner = ArbScanner(bot_cfg)
    opp = await scanner.best_opportunity()

    chains = load_chains()
    token = load_tokens()["VCHF"]
    treasury = TreasuryManager(chains, token, bot_cfg)

    if not opp:
        logger.info("No profitable opportunity (min $%.2f)", bot_cfg.min_profit_usd)
        if scanner.last_selection:
            logger.info("Selection: %s", scanner.last_selection.reason)
        if bot_cfg.platform_vchf_only and bot_cfg.treasury_vchf_home == "platform":
            moved = await treasury.consolidate_vchf_to_platform()
            if moved:
                logger.info("Idle sweep: consolidated %.4f VCHF to platform", moved)
        return

    logger.info(
        "Execute: %s (%s→%s) size=%.1f VCHF profit=$%.2f group=%s dry_run=%s",
        opp.direction,
        opp.buy_chain,
        opp.sell_chain,
        opp.size_vchf,
        opp.net_profit_usd,
        opp.route_group,
        is_dry_run(),
    )
    if opp.base_sol_net is not None or opp.vnx_sol_net is not None:
        logger.info(
            "Parallel scan: base↔sol=$%s vnx↔sol=$%s | %s",
            f"{opp.base_sol_net:.2f}" if opp.base_sol_net is not None else "n/a",
            f"{opp.vnx_sol_net:.2f}" if opp.vnx_sol_net is not None else "n/a",
            opp.selection_reason,
        )

    executor = ArbExecutor(chains, token, bot_cfg)

    async with build_client() as client:
        close_loop = bot_cfg.close_loop_after_cycle
        if close_loop:
            origin = origin_for_direction(opp.direction)
            result = await treasury.run_closed_loop(
                client,
                executor,
                origin=origin,
                direction=opp.direction,
                size_vchf=opp.size_vchf,
            )
            logger.info(
                "Closed loop origin=%s primary=%s closed=%s reason=%s round_p=$%s",
                result.origin,
                result.primary_direction,
                result.closed,
                result.reason,
                f"{result.round_trip_profit_usd:.2f}" if result.round_trip_profit_usd is not None else "n/a",
            )
            if not result.closed and is_vnx_collision_error(result.reason):
                logger.warning(
                    "Closed loop skipped due to VNX platform contention (GBP bot may be active): %s",
                    result.reason,
                )
            if result.primary:
                logger.info(
                    "Primary %s state=%s txs=%s",
                    result.primary.id,
                    result.primary.state,
                    result.primary.tx_hashes,
                )
            if result.return_leg:
                logger.info(
                    "Return %s state=%s txs=%s",
                    result.return_direction,
                    result.return_leg.state,
                    result.return_leg.tx_hashes,
                )
            return

        prep = await treasury.prepare_for_direction(opp.direction, opp.size_vchf)
        if not prep.ready:
            logger.warning("Treasury not ready: %s", prep.notes)
            return
        record = await executor.run_cycle(client, opp.direction, opp.size_vchf)
        await treasury.consolidate_vchf_to_platform()
        if record.error and is_vnx_collision_error(record.error):
            logger.warning(
                "Cycle skipped due to VNX platform contention (GBP bot may be active): %s",
                record.error,
            )
        logger.info("Cycle %s state=%s txs=%s error=%s", record.id, record.state, record.tx_hashes, record.error)


async def main_loop() -> None:
    init_db()
    try:
        ensure_public_key_env()
    except Exception as exc:
        logger.warning("VNX public key not derived: %s", exc)

    bot_cfg = load_bot_config()
    logger.info(
        "VCHF Menace deploy dry_run=%s poll=%ds size=%.0f-%.0f VCHF cctp=%s premium=$%.0f "
        "close_loop=%s always_return=%s platform_vchf_only=%s",
        is_dry_run(),
        bot_cfg.poll_interval_sec,
        bot_cfg.min_trade_vchf,
        bot_cfg.max_trade_vchf,
        bot_cfg.enable_vnx_cctp_routes,
        bot_cfg.indirect_route_premium_usd,
        bot_cfg.close_loop_after_cycle,
        bot_cfg.close_loop_always_return,
        bot_cfg.platform_vchf_only,
    )

    while True:
        try:
            await run_once()
        except Exception as exc:
            if is_vnx_collision_error(str(exc)):
                logger.warning(
                    "Scan cycle skipped — VNX platform contention (GBP bot may be active): %s",
                    exc,
                )
            else:
                logger.exception("Scan cycle error")
        await asyncio.sleep(bot_cfg.poll_interval_sec)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        init_db()
        asyncio.run(run_once())
    else:
        asyncio.run(main_loop())


if __name__ == "__main__":
    main()
