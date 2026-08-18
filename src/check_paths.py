"""DRY_RUN path checker: RPC + VNX + every directed route + every loop (quotes only)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

os.environ["DRY_RUN"] = "true"

from src.config_loader import load_bot_config, load_chains, load_tokens
from src.scanner.routes import ALL_DIRECTIONS, active_loops, route_for_direction
from src.scanner.simulator import simulate_direction
from src.scanner.loop_simulator import simulate_all_loops
from src.vnx.trading import VCHF_MIN_ORDER

TOKEN_SYMBOL = "VCHF"


@dataclass
class CheckRow:
    kind: str
    path: str
    status: str
    net: str = ""
    error: str = ""


@dataclass
class CheckReport:
    rows: list[CheckRow] = field(default_factory=list)
    ok: bool = True

    def add(self, row: CheckRow) -> None:
        self.rows.append(row)
        if row.status == "FAIL":
            self.ok = False


def force_dry_run() -> None:
    os.environ["DRY_RUN"] = "true"


def platform_min() -> float:
    return float(VCHF_MIN_ORDER)


def is_policy_skip(error: str | None) -> bool:
    if not error:
        return False
    e = error.lower()
    return "blocked" in e


def ping_evm(chain_key: str) -> tuple[bool, str]:
    if chain_key == "celo":
        from src.execution.celo_rpc import connect_celo_web3

        w3 = connect_celo_web3()
    elif chain_key == "base":
        from src.execution.base_rpc import connect_base_web3

        w3 = connect_base_web3()
    elif chain_key == "ethereum":
        from src.execution.eth_rpc import connect_eth_web3

        w3 = connect_eth_web3()
    else:
        return False, f"no evm pinger for {chain_key}"
    n = w3.eth.block_number
    return True, f"block {n}"


def ping_solana(rpc_url: str) -> tuple[bool, str]:
    from solana.rpc.api import Client

    client = Client(rpc_url, timeout=20)
    slot = client.get_slot().value
    return True, f"slot {slot}"


async def ping_vnx() -> tuple[bool, str]:
    from src.vnx.client import VnxClient

    async with VnxClient() as vnx:
        data = await vnx.get_quotes()
    quotes = data.get("quotes") or []
    if not quotes:
        return False, "empty quotes"
    return True, f"{len(quotes)} quotes"


def format_table(rows: list[CheckRow]) -> str:
    headers = ("kind", "path", "status", "net", "error")
    data = [headers] + [(r.kind, r.path, r.status, r.net, r.error) for r in rows]
    widths = [max(len(str(row[i])) for row in data) for i in range(5)]
    lines = []
    for i, row in enumerate(data):
        line = "  ".join(str(row[j]).ljust(widths[j]) for j in range(5))
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * widths[j] for j in range(5)))
    return "\n".join(lines)


async def run_check(
    *,
    size: float | None = None,
    ping_rpc: Callable[..., tuple[bool, str]] | None = None,
    ping_vnx_fn: Callable[[], Any] | None = None,
    simulate_dir: Callable[..., Any] | None = None,
    simulate_loops_fn: Callable[..., Any] | None = None,
) -> CheckReport:
    force_dry_run()
    report = CheckReport()
    min_order = platform_min()
    size = float(size) if size is not None else min_order
    if size < min_order:
        report.add(
            CheckRow(
                "size",
                f"{size:g}",
                "FAIL",
                error=f"below platform min order ({min_order:g} {TOKEN_SYMBOL})",
            )
        )
        return report

    chains = load_chains()
    tokens = load_tokens()
    token = tokens[TOKEN_SYMBOL]
    cfg = load_bot_config()

    rpc_chains = [k for k in ("celo", "base", "ethereum", "solana") if k in chains]
    for key in rpc_chains:
        try:
            if ping_rpc is not None:
                ok, detail = ping_rpc(key)
            elif key == "solana":
                ok, detail = ping_solana(chains["solana"].rpc_url)
            else:
                ok, detail = ping_evm(key)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        report.add(CheckRow("rpc", key, "PASS" if ok else "FAIL", error="" if ok else detail))

    try:
        if ping_vnx_fn is not None:
            vnx_ok, vnx_detail = await ping_vnx_fn()
        else:
            vnx_ok, vnx_detail = await ping_vnx()
    except Exception as exc:  # noqa: BLE001
        vnx_ok, vnx_detail = False, str(exc)
    report.add(CheckRow("vnx", "quotes", "PASS" if vnx_ok else "FAIL", error="" if vnx_ok else vnx_detail))

    import httpx

    async with httpx.AsyncClient(timeout=45.0) as client:
        sim_dir = simulate_dir or simulate_direction
        for direction in ALL_DIRECTIONS:
            spec = route_for_direction(direction)
            try:
                sim = await sim_dir(client, chains, token, cfg, direction, size)
            except Exception as exc:  # noqa: BLE001
                report.add(CheckRow("directed", direction, "FAIL", error=str(exc)))
                continue
            err = getattr(sim, "error", None) or ""
            net = getattr(sim, "net_profit_usd", 0.0)
            if err and is_policy_skip(err):
                status = "SKIP"
            elif err:
                status = "FAIL"
            else:
                status = "PASS"
            if (
                spec
                and spec.buy_chain == "vnx"
                and status == "SKIP"
            ):
                status = "FAIL"
                err = err or "vnx_to route must quote"
            report.add(
                CheckRow(
                    "directed",
                    direction,
                    status,
                    net=f"{float(net):.4f}" if status == "PASS" else "",
                    error=err if status != "PASS" else "",
                )
            )

        sim_loops = simulate_loops_fn or simulate_all_loops
        try:
            loop_sims = await sim_loops(client, chains, token, cfg, size)
        except Exception as exc:  # noqa: BLE001
            for loop in active_loops(cfg, token):
                report.add(CheckRow("loop", loop.key, "FAIL", error=str(exc)))
            return report
        for sim in loop_sims:
            err = sim.error or ""
            floors = getattr(sim, "floors_ok", True)
            if err or not floors:
                status = "FAIL"
                if not err and not floors:
                    err = "floors_ok=false"
            else:
                status = "PASS"
            net = getattr(sim, "net_profit_usd", 0.0)
            report.add(
                CheckRow(
                    "loop",
                    sim.loop_key,
                    status,
                    net=f"{float(net):.4f}" if status == "PASS" else "",
                    error=err if status != "PASS" else "",
                )
            )

    expected_loops = {loop.key for loop in active_loops(cfg, token)}
    seen_loops = {r.path for r in report.rows if r.kind == "loop"}
    for missing in sorted(expected_loops - seen_loops):
        report.add(CheckRow("loop", missing, "FAIL", error="not simulated"))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"{TOKEN_SYMBOL} DRY_RUN path checker (RPC + quotes, no broadcast)")
    p.add_argument("--size", type=float, default=None, help=f"token size (default platform min {platform_min():g})")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    force_dry_run()
    args = parse_args(argv)
    report = asyncio.run(run_check(size=args.size))
    if args.json:
        print(json.dumps({"ok": report.ok, "rows": [asdict(r) for r in report.rows]}, indent=2))
    else:
        print(format_table(report.rows))
        fails = [r for r in report.rows if r.status == "FAIL"]
        skips = [r for r in report.rows if r.status == "SKIP"]
        print(
            f"\n{TOKEN_SYMBOL}: {len(report.rows)} rows, "
            f"{len(fails)} FAIL, {len(skips)} SKIP, ok={report.ok}"
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
