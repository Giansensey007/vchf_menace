import os
from unittest.mock import patch

import pytest

from src.vnx.deposits import check_usdc_deposit_amount, min_deposit_usdc


def test_eth_usdc_default_minimum_is_20():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VNX_MIN_DEPOSIT_USDC_ETH", None)
        assert min_deposit_usdc("ETH") == 20.0


def test_min_deposit_usdc_eth_env_override():
    with patch.dict(os.environ, {"VNX_MIN_DEPOSIT_USDC_ETH": "25"}):
        assert min_deposit_usdc("ETH") == 25.0


@pytest.mark.parametrize(
    "amount",
    [19.99, 11.64, 5.0, 0.5],
)
def test_check_usdc_deposit_amount_rejects_below_eth_minimum(amount: float):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VNX_MIN_DEPOSIT_USDC_ETH", None)
        err = check_usdc_deposit_amount("ETH", amount)
    assert err is not None
    assert "20.00" in err
    assert "cumulative" in err.lower()


@pytest.mark.parametrize(
    "amount",
    [20.0, 20.01, 50.0],
)
def test_check_usdc_deposit_amount_accepts_at_or_above_eth_minimum(amount: float):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VNX_MIN_DEPOSIT_USDC_ETH", None)
        assert check_usdc_deposit_amount("ETH", amount) is None
