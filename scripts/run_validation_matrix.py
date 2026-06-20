#!/usr/bin/env python3
"""
Run 20 subagent validation checks × N iterations.
SA-00 runs cross-cutting sanity on every iteration before the other 19 agents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

VALIDATION_DIR = ROOT / "validation"

# SA-00: permanent sanity checker (runs first, every iteration)
SANITY_AGENT = ("SA-00", "sanity-check-all", None)

SUBAGENTS = [
    ("SA-01", "quote-jupiter-sol", "tests/test_quotes.py::test_jupiter_quote_mock"),
    ("SA-02", "quote-celo-onchain", "tests/test_quotes.py::test_onchain_mock"),
    ("SA-03", "quote-vnx-platform", "tests/test_quotes.py::test_vnx_platform_quote_mock"),
    ("SA-04", "quote-router-best", "tests/test_router.py::test_quote_best_onchain"),
    ("SA-05", "routes-six-directions", "tests/test_routes.py::test_all_six_directions"),
    ("SA-06", "route-celo-vnx", "tests/test_routes.py::test_vnx_routes_need_bridge"),
    ("SA-07", "route-sol-vnx", "tests/test_routes.py::test_route_fees_vnx_platform"),
    ("SA-08", "route-celo-sol", "tests/test_routes.py::test_celo_sol_bridge_fee"),
    ("SA-08b", "wormhole-usdt-bridge", "tests/test_wormhole.py::test_wormhole_quote_celo_sol"),
    ("SA-09", "vnx-auth-signing", "tests/test_vnx_auth.py::test_sign_deterministic_payload"),
    ("SA-10", "vnx-bridge-orchestrator", "tests/test_bridge.py::test_bridge_dry_run"),
    ("SA-11", "celo-uniswap-swap", "tests/test_celo.py::test_swap_router_configured"),
    ("SA-12", "solana-jupiter-swap", "tests/test_solana.py::test_keypair_from_base58"),
    ("SA-13", "wallet-celo-hot", "tests/test_celo.py::test_celo_address_from_key"),
    ("SA-14", "wallet-sol-hot", "tests/test_solana.py::test_keypair_roundtrip"),
    ("SA-15", "scanner-all-routes", "tests/test_simulator.py::test_all_directions_registered"),
    ("SA-16", "simulator-pnl", "tests/test_simulator.py::test_pnl_fees_deducted"),
    ("SA-17", "sanity-peg-band", "tests/test_sanity.py::test_peg_and_vchf_rate"),
    ("SA-18", "security-review", "tests/test_security.py::test_no_secrets_in_gitignore"),
    ("SA-19", "db-audit-trail", "tests/test_db.py::test_init_and_save_cycle"),
    ("SA-20", "cctp-iris-fees", "tests/test_cctp.py"),
    ("SA-21", "selector-stress", "tests/test_selector.py tests/test_stress_scanner.py"),
]


def run_pytest(node: str) -> tuple[bool, str]:
    nodes = node.split()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *nodes, "-q", "--tb=short"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    ok = r.returncode == 0
    evidence = (r.stdout + r.stderr)[-500:]
    return ok, evidence


async def run_sanity_agent(iteration: int, live: bool) -> tuple[bool, str]:
    from src.sanity.check import run_full_sanity

    ok, evidence = run_full_sanity()
    parts = [evidence]

    ok_tests, test_ev = run_pytest("tests/test_sanity.py")
    ok = ok and ok_tests
    parts.append(test_ev[-200:] if test_ev else "sanity tests ok")

    if live and iteration >= 2:
        ok_live, live_ev = await run_live_all_routes_check()
        ok = ok and ok_live
        parts.append(f"live: {live_ev}")

    return ok, " | ".join(parts)


async def run_live_all_routes_check() -> tuple[bool, str]:
    try:
        from src.config_loader import load_bot_config, load_chains, load_tokens
        from src.quotes.http_client import build_client
        from src.scanner.routes import active_directions
        from src.scanner.simulator import simulate_direction

        chains = load_chains()
        token = load_tokens()["VCHF"]
        cfg = load_bot_config()
        size = 50.0
        directions = list(active_directions(cfg))
        ok_count = 0

        async with build_client() as client:
            for d in directions:
                sim = await simulate_direction(client, chains, token, cfg, d, size)
                if not sim.error:
                    ok_count += 1

        ok = ok_count == len(directions) and len(directions) >= 2
        evidence = f"active routes {ok_count}/{len(directions)} @ {size} VCHF: {directions}"
        return ok, evidence
    except Exception as exc:
        return False, str(exc)


def write_result(iteration: int, sa_id: str, name: str, status: str, evidence: str) -> None:
    d = VALIDATION_DIR / f"iteration-{iteration}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sa_id}.json").write_text(
        json.dumps({"id": sa_id, "name": name, "status": status, "evidence": evidence[:1500]}, indent=2)
    )


async def run_iteration(iteration: int, live: bool) -> bool:
    all_pass = True

    sa_id, name, _ = SANITY_AGENT
    ok, evidence = await run_sanity_agent(iteration, live)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    write_result(iteration, sa_id, name, status, evidence)
    print(f"  {sa_id} {name}: {status}")

    for sa_id, name, pytest_node in SUBAGENTS:
        ok, evidence = run_pytest(pytest_node)
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        write_result(iteration, sa_id, name, status, evidence)
        print(f"  {sa_id} {name}: {status}")

    return all_pass


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--live", action="store_true")
    args = p.parse_args()

    os.environ.setdefault("DRY_RUN", "true")
    total = args.iterations * (1 + len(SUBAGENTS))

    for i in range(1, args.iterations + 1):
        print(f"\n=== Iteration I{i} ===")
        passed = await run_iteration(i, args.live)
        if not passed:
            print(f"I{i} FAILED")
            sys.exit(1)
        print(f"I{i} all {1 + len(SUBAGENTS)} PASS (incl. SA-00 sanity)")

    print(f"\n{total}/{total} validation passes complete")


if __name__ == "__main__":
    asyncio.run(main())
