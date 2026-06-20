"""Zero-swap guards and infinite-approval wiring."""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.evm_swap import MIN_SWAP_STABLE_OUT_RAW, swap_tokens, validate_swap_min_out


def test_validate_swap_min_out_rejects_zero():
    assert validate_swap_min_out(0) is not None
    assert "zero" in validate_swap_min_out(0).lower()


def test_validate_swap_min_out_rejects_dust():
    assert validate_swap_min_out(MIN_SWAP_STABLE_OUT_RAW - 1) is not None


def test_validate_swap_min_out_accepts_normal():
    assert validate_swap_min_out(MIN_SWAP_STABLE_OUT_RAW) is None


def test_swap_tokens_rejects_zero_amount_in():
    executor = MagicMock()
    chain = MagicMock(kyber_slug="base")
    assert swap_tokens(executor, chain, "0xa", "0xb", 0, MIN_SWAP_STABLE_OUT_RAW) is None
    executor.swap_exact_input.assert_not_called()


def test_celo_approve_if_needed_uses_infinite_allowance():
    from src.execution.celo import CeloExecutor
    from src.execution.token_approvals import MAX_UINT256

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_w3.eth.contract.return_value = mock_contract
    mock_contract.functions.allowance.return_value.call.return_value = 0
    mock_fn = MagicMock()
    mock_contract.functions.approve.return_value = mock_fn
    mock_fn.build_transaction.return_value = {"gas": 1}

    with patch.object(CeloExecutor, "_build_and_send", return_value="0xtx"):
        with patch.object(CeloExecutor, "_tx_base", return_value={"gas": 1}):
            ex = CeloExecutor.__new__(CeloExecutor)
            ex.w3 = mock_w3
            ex.account = MagicMock(address="0x" + "11" * 20)
            ex.approve_if_needed("0x" + "aa" * 20, "0x" + "bb" * 20, 1_000_000)

    approve_call = mock_contract.functions.approve.call_args
    assert approve_call[0][1] == MAX_UINT256


def test_approve_infinite_erc20_uses_max_uint():
    from src.execution.token_approvals import MAX_UINT256, approve_infinite_erc20

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_w3.eth.contract.return_value = mock_contract
    mock_contract.functions.allowance.return_value.call.return_value = 0
    mock_fn = MagicMock()
    mock_contract.functions.approve.return_value = mock_fn
    mock_fn.build_transaction.return_value = {"gas": 1}

    executor = MagicMock()
    executor.w3 = mock_w3
    executor.account = MagicMock(address="0x" + "22" * 20)

    with patch("src.execution.token_approvals.is_dry_run", return_value=False):
        with patch("src.execution.token_approvals._build_and_send", return_value="0xtx"):
            with patch("src.execution.token_approvals._tx_base", return_value={"gas": 1}):
                approve_infinite_erc20(executor, "0x" + "aa" * 20, "0x" + "bb" * 20)

    approve_call = mock_contract.functions.approve.call_args
    assert approve_call[0][1] == MAX_UINT256
