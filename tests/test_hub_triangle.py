"""Hub triangle: BASE ↔ ETH ↔ SOL composite paths."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bridge.cctp import CctpBridgeResult
from src.bridge.hub_eth import base_usdc_to_sol_usdc, sol_usdc_to_base_usdc
from src.bridge.wormhole import WormholeBridgeResult


@pytest.mark.asyncio
async def test_base_eth_sol_composite_success():
    wh_br = WormholeBridgeResult("base_eth", 5.0, "0xwh", "0xredeem", False, True)
    cctp_br = CctpBridgeResult("eth_sol", 4.9, "0xcctp", "solsig", False, True)

    with (
        patch("src.bridge.hub_eth.wormhole_base_to_eth", new_callable=AsyncMock) as mock_wh,
        patch("src.bridge.hub_eth.swap_eth_usdt_to_usdc", new_callable=AsyncMock) as mock_swap,
        patch("src.bridge.cctp.CircleCctpBridge") as mock_cctp_cls,
    ):
        mock_wh.return_value = {"success": True, "wormhole": wh_br}
        mock_swap.return_value = {"success": True, "tx": "0xswap", "expected_usdc": 4.95}
        mock_cctp_cls.return_value.bridge_usdc_eth_to_sol = AsyncMock(return_value=cctp_br)

        r = await base_usdc_to_sol_usdc(None, 5.0)
    assert r["success"]
    assert r["direction"] == "base_usdc_to_sol_usdc"
    mock_cctp_cls.return_value.bridge_usdc_eth_to_sol.assert_awaited_once()


@pytest.mark.asyncio
async def test_sol_eth_base_composite_success():
    wh_br = WormholeBridgeResult("eth_base", 4.9, "0xwh2", "0xbase", False, True)
    cctp_br = CctpBridgeResult("sol_eth", 5.0, "solburn", "0xclaim", False, True)

    with (
        patch("src.bridge.cctp.CircleCctpBridge") as mock_cctp_cls,
        patch("src.bridge.hub_eth.swap_eth_usdc_to_usdt", new_callable=AsyncMock) as mock_swap,
        patch("src.bridge.hub_eth.wormhole_eth_to_base", new_callable=AsyncMock) as mock_wh,
    ):
        mock_cctp_cls.return_value.bridge_usdc_sol_to_eth = AsyncMock(return_value=cctp_br)
        mock_swap.return_value = {"success": True, "tx": "0xswap2", "expected_usdt": 4.95}
        mock_wh.return_value = {"success": True, "wormhole": wh_br}

        r = await sol_usdc_to_base_usdc(None, 5.0)
    assert r["success"]
    assert r["direction"] == "sol_usdc_to_base_usdc"
