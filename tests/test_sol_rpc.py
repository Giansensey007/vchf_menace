"""Solana RPC throttle and 429 backoff."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.execution import sol_rpc
from src.quotes import sync_throttle


def test_data_dir_follows_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.config_loader import data_dir, db_path

    db = tmp_path / "persist" / "bot.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert data_dir() == db_path().parent
    assert data_dir().name == "persist"


def test_sol_rpc_backoff_grows_with_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOL_RPC_429_BACKOFF_SEC", "8")
    monkeypatch.setenv("API_RETRY_BACKOFF_CAP_SEC", "120")
    first = sol_rpc.sol_rpc_backoff_sec(0)
    second = sol_rpc.sol_rpc_backoff_sec(1)
    assert 8.0 <= first <= 8.5
    assert 16.0 <= second <= 16.5
    assert second > first


def test_sol_rpc_min_interval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_throttle.reset_sync_throttle()
    monkeypatch.setenv("SOL_RPC_MIN_INTERVAL_MS", "900")
    monkeypatch.delenv("API_SYNC_SOLANA_RPC_MS", raising=False)
    assert sync_throttle._interval_sec("solana_rpc") == pytest.approx(0.9)


def test_default_solana_rpc_interval_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOL_RPC_MIN_INTERVAL_MS", raising=False)
    monkeypatch.delenv("API_SYNC_SOLANA_RPC_MS", raising=False)
    assert sync_throttle._interval_sec("solana_rpc") >= 0.8
