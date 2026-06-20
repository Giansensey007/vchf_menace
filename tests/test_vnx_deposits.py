import os
from unittest.mock import patch

import pytest

from src.vnx.deposits import check_usdc_deposit_amount, min_deposit_usdc, validate_eth_usdc_vnx_deposit
from src.vnx.constants import (
    CELO_HUB_STABLE,
    ETH_HUB_STABLE,
    VNX_ETH_DEPOSIT_ASSET,
    check_eth_hub_stable_for_vnx,
    check_vnx_eth_deposit_asset,
)


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


def test_vnx_eth_deposit_asset_constant():
    assert VNX_ETH_DEPOSIT_ASSET == "USDC"
    assert ETH_HUB_STABLE == "USDC"
    assert CELO_HUB_STABLE == "USDT"


def test_check_vnx_eth_deposit_asset_rejects_usdt_on_eth():
    err = check_vnx_eth_deposit_asset("USDT", "ETH")
    assert err is not None
    assert "USDC only" in err


def test_check_vnx_eth_deposit_asset_accepts_usdc_on_eth():
    assert check_vnx_eth_deposit_asset("USDC", "ETH") is None


def test_check_vnx_eth_deposit_asset_ignores_usdt_on_celo():
    assert check_vnx_eth_deposit_asset("USDT", "CELO") is None


def test_validate_eth_usdc_vnx_deposit_rejects_usdt_asset():
    err = validate_eth_usdc_vnx_deposit(25.0, asset="USDT", blockchain="ETH")
    assert err is not None
    assert "USDC only" in err


def test_validate_eth_usdc_vnx_deposit_accepts_usdc_at_minimum():
    assert validate_eth_usdc_vnx_deposit(20.0) is None
