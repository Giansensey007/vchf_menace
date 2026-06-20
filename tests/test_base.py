import os

import pytest
from unittest.mock import MagicMock, patch


def test_base_address_from_key():
    os.environ["BASE_PRIVATE_KEY"] = "0x" + "11" * 32
    from src.execution.base import BaseExecutor
    from src.config_loader import load_chains

    mock_w3 = MagicMock()
    mock_w3.is_connected.return_value = True
    mock_w3.eth.get_balance.return_value = 0

    with patch("src.execution.base.Web3", return_value=mock_w3):
        ex = BaseExecutor(load_chains()["base"])
    assert ex.address.startswith("0x")
    assert len(ex.address) == 42


def test_swap_router_configured():
    from src.config_loader import load_chains

    base = load_chains()["base"]
    assert base.swap_router is not None
    assert base.quoter_v2 is not None
