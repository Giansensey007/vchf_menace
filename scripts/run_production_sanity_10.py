#!/usr/bin/env python3
"""10-iteration production sanity: pytest + audit + verify-all (even iters)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
os.chdir(ROOT)
os.environ.setdefault("DRY_RUN", "true")

OUT = ROOT / "validation" / "production-sanity-10.json"


def run_pytest() -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
        capture_output=True,
        text=True,
    )
    tail = (r.stdout + r.stderr).strip().splitlines()
    summary = tail[-1] if tail else "no output"
    passed = r.returncode == 0 and "failed" not in summary.lower()
    m = re.search(r"(\d+) passed", summary)
    count = int(m.group(1)) if m else 0
    return passed, summary, count


async def run_audit() -> tuple[bool, list[str]]:
    from scripts.execute_route_matrix import audit

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            await audit()
        ok = True
    except Exception as exc:
        buf.write(str(exc))
        ok = False
    lines = buf.getvalue().strip().splitlines()
    return ok, lines[-5:]


async def run_verify_all() -> tuple[bool, list[str]]:
    from scripts.execute_route_matrix import step_verify_all

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            ok = await step_verify_all()
    except Exception as exc:
        buf.write(str(exc))
        ok = False
    lines = buf.getvalue().strip().splitlines()
    highlights = [ln for ln in lines if "PASS" in ln or "FAIL" in ln or "SKIP" in ln][-12:]
    return ok, highlights


async def main() -> int:
    results: list[dict] = []
    test_count = 0
    print("=== VCHF Menace: 10-iteration production sanity (round 3) ===\n")

    for i in range(1, 11):
        print(f"--- Iteration {i}/10 ---")
        entry: dict = {"iteration": i, "pytest": None, "audit": None, "verify_all": None}

        p_ok, p_sum, p_count = run_pytest()
        test_count = p_count
        entry["pytest"] = {"pass": p_ok, "summary": p_sum}
        print(f"  pytest: {'PASS' if p_ok else 'FAIL'} — {p_sum}")

        a_ok, a_tail = await run_audit()
        entry["audit"] = {"pass": a_ok, "tail": a_tail}
        print(f"  audit:  {'PASS' if a_ok else 'FAIL'}")

        if i % 2 == 0:
            v_ok, v_hi = await run_verify_all()
            entry["verify_all"] = {"pass": v_ok, "highlights": v_hi}
            print(f"  verify-all: {'PASS' if v_ok else 'FAIL/SKIP'}")
        else:
            print("  verify-all: (skipped)")

        results.append(entry)
        print()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test_count": test_count,
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {OUT}")

    all_pytest = all(r["pytest"]["pass"] for r in results)
    all_audit = all(r["audit"]["pass"] for r in results)
    verify_iters = [r for r in results if r["verify_all"]]
    all_verify = all(r["verify_all"]["pass"] for r in verify_iters)
    print(f"\nFinal: pytest={all_pytest} audit={all_audit} verify-all={all_verify} ({test_count} tests)")
    return 0 if all_pytest and all_audit and all_verify else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
