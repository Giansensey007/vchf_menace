"""Tests for one-time infinite approvals (no per-trade approve in swap hot path)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from src.execution.evm_swap import MIN_SWAP_STABLE_OUT_RAW
from src.execution.token_approvals import (
    MAX_UINT256,
    check_allowance,
    collect_approval_targets,
    is_infinite_allowance,
)
from src.config_loader import load_bridge_config, load_chains, load_tokens


def test_is_infinite_allowance():
    assert is_infinite_allowance(MAX_UINT256)
    assert not is_infinite_allowance(10**18)


def test_check_allowance_ok_when_max():
    w3 = MagicMock()
    w3.eth.contract.return_value.functions.allowance.return_value.call.return_value = MAX_UINT256
    assert check_allowance(w3, "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40, 10**18) is None


def test_base_swap_does_not_call_approve_when_allowance_max():
    os.environ["BASE_PRIVATE_KEY"] = "0x" + "11" * 32
    from src.execution.base import BaseExecutor

    mock_w3 = MagicMock()
    mock_w3.is_connected.return_value = True
    mock_w3.eth.get_transaction_count.return_value = 0
    mock_w3.eth.gas_price = 1
    mock_router = MagicMock()
    mock_w3.eth.contract.side_effect = lambda address, abi: (
        mock_router if "exactInput" in str(abi) else MagicMock()
    )
    mock_router.functions.exactInputSingle.return_value.build_transaction.return_value = {"to": "0xrouter"}

    with patch("src.execution.base.connect_base_web3", return_value=mock_w3):
        with patch("src.execution.token_approvals.check_allowance", return_value=None) as mock_check:
            ex = BaseExecutor(load_chains()["base"])
            ex._build_and_send = MagicMock(return_value="0xswap")
            ex.swap_exact_input(
                "0x" + "a" * 40,
                "0x" + "b" * 40,
                10**18,
                MIN_SWAP_STABLE_OUT_RAW,
            )
    mock_check.assert_called_once()


def test_kyber_swap_checks_allowance_not_approve():
    from src.execution.kyber_swap import swap_via_kyber

    executor = MagicMock()
    executor.account.address = "0x" + "1" * 40
    executor.chain.kyber_slug = "base"
    executor.last_error = None
    executor._build_and_send.return_value = "0xtx"

    built = {"routerAddress": "0x" + "3" * 40, "data": "0x", "gas": 500000}
    with patch("src.execution.kyber_swap.fetch_route", return_value=({"x": 1}, 10**6)):
        with patch("src.execution.kyber_swap.build_swap_tx", return_value=built):
            with patch("src.execution.token_approvals.check_allowance", return_value=None) as mock_check:
                swap_via_kyber(executor, "0x" + "a" * 40, "0x" + "b" * 40, 10**6, MIN_SWAP_STABLE_OUT_RAW)
    mock_check.assert_called_once()
    executor.approve_if_needed.assert_not_called()


def test_collect_approval_targets_vchf():
    chains = load_chains()
    token = load_tokens()["VCHF"]
    bridge = load_bridge_config()
    targets = collect_approval_targets(chains, token, bridge)
    base_targets = [t for t in targets if t.chain_key == "base"]
    assert len(base_targets) >= 3
    labels = {t.label for t in base_targets}
    assert any("Kyber" in lbl for lbl in labels)
    assert any("Wormhole" in lbl for lbl in labels)
