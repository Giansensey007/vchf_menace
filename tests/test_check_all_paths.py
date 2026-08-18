"""Mocked DRY_RUN all-paths checker (no live RPC)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from src.check_paths import TOKEN_SYMBOL, platform_min, run_check
from src.config_loader import load_bot_config, load_chains, load_tokens
from src.scanner.routes import ALL_DIRECTIONS, active_loops, route_for_direction


def _expected_row_count() -> int:
    chains = load_chains()
    n_rpc = len([k for k in ("celo", "base", "ethereum", "solana") if k in chains])
    token = load_tokens()[TOKEN_SYMBOL]
    return n_rpc + 1 + len(ALL_DIRECTIONS) + len(active_loops(load_bot_config(), token))


async def _ok_dir(_client, _chains, _token, _cfg, direction, _size):
    spec = route_for_direction(direction)
    if spec and spec.buy_chain != "vnx":
        return SimpleNamespace(
            error=f"on-chain {TOKEN_SYMBOL} buy on {spec.buy_chain} blocked (platform_only)",
            net_profit_usd=0.0,
            floors_ok=True,
        )
    return SimpleNamespace(error=None, net_profit_usd=0.12, floors_ok=True)


async def _ok_loops(_client, _chains, token, cfg, _size):
    return [
        SimpleNamespace(loop_key=loop.key, error=None, floors_ok=True, net_profit_usd=-0.4)
        for loop in active_loops(cfg, token)
    ]


async def _ok_vnx():
    return True, "n quotes"


async def _run(**kwargs):
    return await run_check(
        ping_rpc=lambda _key: (True, "ok"),
        ping_vnx_fn=_ok_vnx,
        simulate_dir=_ok_dir,
        simulate_loops_fn=_ok_loops,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_row_counts_policy_skip_and_dry_run_forced(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    report = await _run()
    assert os.environ["DRY_RUN"] == "true"
    assert len(report.rows) == _expected_row_count()
    directed = [r for r in report.rows if r.kind == "directed"]
    assert len(directed) == len(ALL_DIRECTIONS)
    vnx_to = [r for r in directed if r.path.startswith("vnx_to_")]
    blocked = [r for r in directed if not r.path.startswith("vnx_to_")]
    assert vnx_to and all(r.status == "PASS" for r in vnx_to)
    assert blocked and all(r.status == "SKIP" for r in blocked)
    loops = [r for r in report.rows if r.kind == "loop"]
    assert len(loops) == len(active_loops(load_bot_config(), load_tokens()[TOKEN_SYMBOL]))
    assert all(r.status == "PASS" for r in loops)
    assert report.ok


@pytest.mark.asyncio
async def test_vnx_to_must_quote_not_skip():
    async def all_blocked(_c, _ch, _t, _cfg, direction, _s):
        return SimpleNamespace(error="on-chain buy blocked (policy)", net_profit_usd=0.0)

    report = await run_check(
        ping_rpc=lambda _k: (True, "ok"),
        ping_vnx_fn=_ok_vnx,
        simulate_dir=all_blocked,
        simulate_loops_fn=_ok_loops,
    )
    vnx_to = [r for r in report.rows if r.kind == "directed" and r.path.startswith("vnx_to_")]
    assert vnx_to and all(r.status == "FAIL" for r in vnx_to)
    assert not report.ok


@pytest.mark.asyncio
async def test_loop_error_fails_runner():
    async def one_error(_c, _ch, token, cfg, _s):
        rows = []
        for i, loop in enumerate(active_loops(cfg, token)):
            err = "quote failed" if i == 0 else None
            rows.append(
                SimpleNamespace(loop_key=loop.key, error=err, floors_ok=True, net_profit_usd=0.0)
            )
        return rows

    report = await run_check(
        ping_rpc=lambda _k: (True, "ok"),
        ping_vnx_fn=_ok_vnx,
        simulate_dir=_ok_dir,
        simulate_loops_fn=one_error,
    )
    fails = [r for r in report.rows if r.kind == "loop" and r.status == "FAIL"]
    assert len(fails) == 1
    assert "quote failed" in fails[0].error
    assert not report.ok


@pytest.mark.asyncio
async def test_floors_ok_false_fails_runner():
    async def one_floor(_c, _ch, token, cfg, _s):
        rows = []
        for i, loop in enumerate(active_loops(cfg, token)):
            rows.append(
                SimpleNamespace(
                    loop_key=loop.key,
                    error=None,
                    floors_ok=(i != 0),
                    net_profit_usd=0.0,
                )
            )
        return rows

    report = await run_check(
        ping_rpc=lambda _k: (True, "ok"),
        ping_vnx_fn=_ok_vnx,
        simulate_dir=_ok_dir,
        simulate_loops_fn=one_floor,
    )
    fails = [r for r in report.rows if r.kind == "loop" and r.status == "FAIL"]
    assert len(fails) == 1
    assert "floors_ok" in fails[0].error
    assert not report.ok


@pytest.mark.asyncio
async def test_size_below_platform_min_rejected():
    report = await run_check(size=platform_min() - 0.01)
    assert len(report.rows) == 1
    assert report.rows[0].kind == "size"
    assert report.rows[0].status == "FAIL"
    assert not report.ok


@pytest.mark.asyncio
async def test_size_at_platform_min_accepted():
    report = await _run(size=platform_min())
    assert all(r.kind != "size" for r in report.rows)
    assert len(report.rows) == _expected_row_count()
    assert report.ok


@pytest.mark.prelaunch
@pytest.mark.asyncio
async def test_live_rpc_and_quotes():
    report = await run_check()
    fails = [r for r in report.rows if r.status == "FAIL"]
    assert report.ok, fails
