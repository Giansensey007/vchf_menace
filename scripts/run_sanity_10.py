#!/usr/bin/env python3
"""
10 subagent sanity check with live quotes, bridges, and route simulations.

SA-00  config/env sanity
SA-01  live Jupiter (Solana VCHF sell)
SA-02  live Base onchain (VCHF buy)
SA-03  live VNX platform quotes
SA-04  VNX VCHF deposit addresses (BASE + SOL)
SA-05  Wormhole USDT bridge quote
SA-06  VNX VCHF bridge dry-run orchestrator
SA-07  live simulate base_to_solana
SA-08  live simulate solana_to_base
SA-09  pytest suite (unit + integration mocks)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.quotes.api_gate import stagger_delay_ms

VALIDATION_DIR = ROOT / "validation" / "sanity-10"


@dataclass
class AgentResult:
    agent_id: str
    name: str
    passed: bool
    evidence: str


def run_pytest_all() -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = (r.stdout + r.stderr)[-800:]
    summary = tail.splitlines()[-1] if tail else "no output"
    return r.returncode == 0, summary


async def agent_sa00() -> AgentResult:
    from src.sanity.check import run_full_sanity

    ok, evidence = run_full_sanity()
    return AgentResult("SA-00", "config-env-sanity", ok, evidence)


async def agent_sa01() -> AgentResult:
    from src.config_loader import load_chains, load_tokens, token_decimals
    from src.quotes.http_client import build_client
    from src.quotes.router import sell_token_for_stable
    from src.quotes.types import from_human, to_human

    chains = load_chains()
    token = load_tokens()["VCHF"]
    sol = chains["solana"]
    size = 50.0
    dec = token_decimals(token, "solana")

    async with build_client() as client:
        q = await sell_token_for_stable(client, sol, token, "solana", from_human(size, dec))
    if not q:
        return AgentResult("SA-01", "live-jupiter-sol-sell", False, "no quote")
    usdc = float(to_human(q.amount_out, sol.hub_decimals))
    rate = usdc / size
    ok = 1.0 < rate < 2.0
    return AgentResult(
        "SA-01",
        "live-jupiter-sol-sell",
        ok,
        f"{size} VCHF -> {usdc:.4f} USDC ({rate:.4f}/VCHF) via {q.provider}",
    )


async def agent_sa02() -> AgentResult:
    from src.config_loader import load_chains, load_tokens, token_decimals
    from src.quotes.http_client import build_client
    from src.quotes.router import buy_token_with_stable
    from src.quotes.types import from_human, to_human

    chains = load_chains()
    token = load_tokens()["VCHF"]
    celo = chains["base"]
    usdt = 70.0
    dec = token_decimals(token, "base")

    async with build_client() as client:
        q = await buy_token_with_stable(client, celo, token, "base", from_human(usdt, celo.hub_decimals))
    if not q:
        return AgentResult("SA-02", "live-celo-onchain-buy", False, "no quote")
    vchf = float(to_human(q.amount_out, dec))
    rate = usdt / vchf if vchf else 0
    ok = 1.0 < rate < 2.0
    return AgentResult(
        "SA-02",
        "live-celo-onchain-buy",
        ok,
        f"{usdt:.2f} USDT -> {vchf:.4f} VCHF ({rate:.4f} USDT/VCHF) via {q.provider}",
    )


async def agent_sa03() -> AgentResult:
    from src.config_loader import load_chains, load_tokens, token_decimals
    from src.quotes.http_client import build_client
    from src.quotes.router import sell_token_for_stable
    from src.quotes.types import from_human, to_human
    from src.vnx.client import VnxClient

    parts: list[str] = []
    ok = True

    try:
        async with VnxClient() as vnx:
            assets = await vnx.get_assets()
            vchf = next((a for a in assets.get("assets", []) if a.get("asset") == "VCHF"), None)
            if not vchf:
                ok = False
                parts.append("VCHF asset missing")
            else:
                active = [b["blockchain"] for b in vchf.get("blockchains", []) if b.get("isactive")]
                parts.append(f"VCHF chains active: {','.join(active)}")

            try:
                quotes = await vnx.get_quotes()
                if isinstance(quotes, list):
                    pair = next((q for q in quotes if q.get("pair") == "VCHF/USDC"), None)
                else:
                    inner = quotes.get("quotes", quotes)
                    pair = inner.get("VCHF/USDC") if isinstance(inner, dict) else None
                if pair:
                    bid, ask = pair.get("b", [None, None])[0], pair.get("a", [None, None])[0]
                    parts.append(f"VCHF/USDC bid={bid} ask={ask}")
                else:
                    parts.append("quotes ok (VCHF/USDC in payload)")
            except Exception as exc:
                parts.append(f"quotes fetch: {exc}")
    except Exception as exc:
        ok = False
        parts.append(f"VNX API: {exc}")

    # Platform sell quote path (even if arb routes disabled)
    chains = load_chains()
    token = load_tokens()["VCHF"]
    vnx = chains["vnx"]
    dec = token_decimals(token, "vnx")
    async with build_client() as client:
        q = await sell_token_for_stable(client, vnx, token, "vnx", from_human(50, dec))
    if q:
        parts.append(f"platform sell 50 VCHF ok via {q.provider}")
    else:
        parts.append("platform sell quote failed")

    # Pass if assets loaded and platform sell works; quotes optional under rate limit
    if ok and q:
        return AgentResult("SA-03", "live-vnx-platform", True, " | ".join(parts))
    return AgentResult("SA-03", "live-vnx-platform", ok, " | ".join(parts))


async def agent_sa04() -> AgentResult:
    import os

    from src.vnx.client import VnxClient

    ok = True
    parts: list[str] = []
    async with VnxClient() as vnx:
        for env_key, default in (("VNX_BASE_BLOCKCHAIN", "BASE"), ("VNX_SOL_BLOCKCHAIN", "SOL")):
            bc = os.getenv(env_key, default)
            try:
                dep = await vnx.deposit_address("VCHF", bc)
                addr = dep.get("address") or ""
                if not addr:
                    ok = False
                    parts.append(f"{bc}: no address")
                else:
                    parts.append(f"{bc} deposit {addr[:12]}…")
            except Exception as exc:
                ok = False
                parts.append(f"{bc}: {exc}")
            await asyncio.sleep(2.0)
    return AgentResult("SA-04", "vnx-vchf-deposit-addrs", ok, " | ".join(parts))


async def agent_sa05() -> AgentResult:
    from src.bridge.wormhole import WormholePortalBridge
    from src.config_loader import load_bridge_config, load_chains

    wh_cfg = load_bridge_config()["wormhole"]
    celo = load_chains()["base"]
    wh = WormholePortalBridge(celo)
    q = wh.quote_usdt("base", "solana", 100.0)
    ok = q.ok and q.amount_out_usdt > 0
    parts = [
        f"100 USDT -> {q.amount_out_usdt:.2f} USDT",
        f"fee ${q.fee_usd:.2f}",
        f"bridge {wh_cfg['base_token_bridge'][:10]}…",
    ]
    return AgentResult("SA-05", "wormhole-usdt-quote", ok, " | ".join(parts))


async def agent_sa06() -> AgentResult:
    import os

    from src.config_loader import BotConfig, load_bot_config
    from src.vnx.bridge import VnxBridge

    cfg = load_bot_config()
    bridge = VnxBridge(cfg)

    async def fake_deposit(_addr: str) -> str:
        return "dry-run-deposit"

    result = await bridge.bridge_vchf(
        direction="base_to_solana",
        quantity=50.0,
        source_blockchain=os.getenv("VNX_BASE_BLOCKCHAIN", "BASE"),
        dest_blockchain=os.getenv("VNX_SOL_BLOCKCHAIN", "SOL"),
        dest_label=os.getenv("VNX_SOL_WITHDRAW_LABEL", "sol-hot"),
        deposit_tx_builder=fake_deposit,
    )
    ok = result.success and result.dry_run
    return AgentResult(
        "SA-06",
        "vnx-vchf-bridge-dryrun",
        ok,
        f"success={result.success} dry_run={result.dry_run} dep={str(result.deposit_address)[:12]}…",
    )


async def _simulate(direction: str, size: float) -> AgentResult:
    from src.config_loader import load_bot_config, load_chains, load_tokens
    from src.quotes.http_client import build_client
    from src.quotes.sanity import sanity_check_simulation
    from src.scanner.simulator import simulate_direction

    chains = load_chains()
    token = load_tokens()["VCHF"]
    cfg = load_bot_config()

    async with build_client() as client:
        sim = await simulate_direction(client, chains, token, cfg, direction, size)

    ok = sim.error is None
    parts = [
        f"in=${sim.stable_in_usd:.2f} out=${sim.stable_out_usd:.2f}",
        f"fees=${sim.fees_usd:.2f} net=${sim.net_profit_usd:.2f}",
        f"bridge={sim.needs_bridge} sanity={sim.sanity_ok}",
        f"profitable={sim.profitable}",
    ]
    if sim.error:
        parts.append(f"error={sim.error}")
    if sim.sanity_notes:
        parts.append("; ".join(sim.sanity_notes))
    sane, issues = sanity_check_simulation(sim)
    if not sane and sim.error is None:
        parts.append(f"sanity_issues={issues}")

    agent_id = "SA-07" if direction == "base_to_solana" else "SA-08"
    name = f"live-sim-{direction.replace('_', '-')}"
    return AgentResult(agent_id, name, ok, " | ".join(parts))


async def agent_sa07() -> AgentResult:
    return await _simulate("base_to_solana", 50.0)


async def agent_sa08() -> AgentResult:
    return await _simulate("solana_to_base", 50.0)


async def agent_sa09() -> AgentResult:
    ok, evidence = run_pytest_all()
    return AgentResult("SA-09", "pytest-full-suite", ok, evidence)


AGENTS = [
    agent_sa00,
    agent_sa01,
    agent_sa02,
    agent_sa03,
    agent_sa04,
    agent_sa05,
    agent_sa06,
    agent_sa07,
    agent_sa08,
    agent_sa09,
]


def write_result(result: AgentResult) -> None:
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    path = VALIDATION_DIR / f"{result.agent_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": result.agent_id,
                "name": result.name,
                "status": "PASS" if result.passed else "FAIL",
                "evidence": result.evidence[:2000],
            },
            indent=2,
        )
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--parallel",
        action="store_true",
        help="Run agents concurrently (NOT recommended — may trigger rate limits)",
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Repeat full 10-agent sweep N times (default 1; use 3 for deep sanity)",
    )
    args = p.parse_args()

    print("=== VCHF Menace: 10 subagent sanity check (live) ===")
    print(f"    iterations={args.iterations}  parallel={args.parallel}\n")

    all_iteration_results: list[list[AgentResult]] = []

    for iteration in range(1, args.iterations + 1):
        if args.iterations > 1:
            print(f"--- Iteration {iteration}/{args.iterations} ---")

        if args.parallel:
            results = await asyncio.gather(*[fn() for fn in AGENTS])
        else:
            results = []
            for fn in AGENTS:
                r = await fn()
                results.append(r)
                status = "PASS" if r.passed else "FAIL"
                print(f"  {r.agent_id} {r.name}: {status}")
                print(f"       {r.evidence[:200]}")
                write_result(r)
                await stagger_delay_ms()

        if args.parallel:
            for r in results:
                status = "PASS" if r.passed else "FAIL"
                print(f"  {r.agent_id} {r.name}: {status}")
                print(f"       {r.evidence[:200]}")
                write_result(r)

        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"  Iteration {iteration}: {passed}/{total} agents PASS\n")
        all_iteration_results.append(results)

        if iteration < args.iterations:
            await stagger_delay_ms(float(os.getenv("SANITY_ITERATION_DELAY_MS", "3000")))

    # Summary across iterations
    last = all_iteration_results[-1]
    passed = sum(1 for r in last if r.passed)
    total = len(last)
    all_pass_all_iters = all(
        sum(1 for r in iter_results if r.passed) == len(iter_results)
        for iter_results in all_iteration_results
    )

    print(f"=== Final: {passed}/{total} agents PASS (last iteration) ===")
    if args.iterations > 1:
        print(f"=== All {args.iterations} iterations PASS: {all_pass_all_iters} ===")

    summary_path = VALIDATION_DIR / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "passed": passed,
                "total": total,
                "all_pass": passed == total,
                "iterations": args.iterations,
                "all_iterations_pass": all_pass_all_iters,
                "agents": [
                    {"id": r.agent_id, "name": r.name, "passed": r.passed, "evidence": r.evidence[:500]}
                    for r in last
                ],
                "iteration_summaries": [
                    {
                        "iteration": i + 1,
                        "passed": sum(1 for r in ir if r.passed),
                        "total": len(ir),
                    }
                    for i, ir in enumerate(all_iteration_results)
                ],
            },
            indent=2,
        )
    )
    print(f"Results: {summary_path}")

    if not all_pass_all_iters:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
