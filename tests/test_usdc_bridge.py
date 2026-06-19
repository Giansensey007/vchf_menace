import pytest

from src.config_loader import load_bot_config
from src.vnx.deposits import check_usdc_deposit_amount, min_deposit_usdc


def test_min_usdc_deposit_eth():
    assert min_deposit_usdc("ETH") == 20.0


def test_check_usdc_deposit_blocks_sub_min():
    err = check_usdc_deposit_amount("ETH", 19.0)
    assert err and "20.00" in err


def test_check_usdc_deposit_allows_above_min():
    assert check_usdc_deposit_amount("ETH", 20.0) is None


@pytest.mark.asyncio
async def test_usdc_bridge_dry_run_deposit():
    from src.vnx.usdc_bridge import VnxUsdcBridge

    cfg = load_bot_config()
    bridge = VnxUsdcBridge(cfg)

    async def builder(_addr: str) -> str:
        return "dry-run-tx"

    br = await bridge.deposit_usdc(25.0, deposit_tx_builder=builder)
    assert br.success
    assert br.dry_run
