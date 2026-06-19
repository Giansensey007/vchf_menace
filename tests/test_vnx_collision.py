"""Tests for VNX platform collision error classification."""

import os

import pytest

from src.vnx.collision import (
    collision_backoff_sec,
    collision_retry_max,
    is_vnx_collision_error,
)


@pytest.mark.parametrize(
    "msg",
    [
        "invalid_request_limit exceeded",
        "Another order in flight",
        "insufficient platform VCHF (100.00 < 200.00)",
        "order rejected: concurrent request",
        "withdraw rejected — busy",
    ],
)
def test_is_vnx_collision_error_positive(msg: str) -> None:
    assert is_vnx_collision_error(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "",
        None,
        "timeout waiting for VCHF on Celo",
        "not profitable",
        "unsupported direction",
    ],
)
def test_is_vnx_collision_error_negative(msg: str | None) -> None:
    assert not is_vnx_collision_error(msg)


def test_collision_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VNX_COLLISION_RETRY_MAX", raising=False)
    monkeypatch.delenv("VNX_COLLISION_BACKOFF_SEC", raising=False)
    assert collision_retry_max() == 3
    assert collision_backoff_sec(0) == 5.0
    assert collision_backoff_sec(2) == 15.0


def test_collision_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_COLLISION_RETRY_MAX", "5")
    monkeypatch.setenv("VNX_COLLISION_BACKOFF_SEC", "2")
    assert collision_retry_max() == 5
    assert collision_backoff_sec(1) == 4.0
