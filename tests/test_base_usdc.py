from unittest.mock import MagicMock, patch

from src.bridge.base_usdc import base_usdc_addresses, consolidate_wrapped_to_canonical


def test_base_usdc_addresses_match_config():
    canonical, wrapped = base_usdc_addresses()
    assert canonical.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert wrapped.lower() == canonical.lower()


def test_consolidate_skips_when_no_wrapped():
    base = MagicMock()
    base.balance_erc20.return_value = 0
    r = consolidate_wrapped_to_canonical(base=base)
    assert r["success"] is True
    assert r["skipped"] is True


def test_consolidate_swaps_wrapped_to_canonical():
    base = MagicMock()
    base.balance_erc20.return_value = 5_000_000
    base.simulate_swap.return_value = {"amount_out": 4_990_000}
    base.swap_exact_input.return_value = "0xabc"
    with patch("src.bridge.base_usdc.base_usdc_addresses", return_value=("0xcanon", "0xwrap")):
        r = consolidate_wrapped_to_canonical(5.0, base=base)
    assert r["success"] is True
    assert r["tx"] == "0xabc"
    base.swap_exact_input.assert_called_once()
    args = base.swap_exact_input.call_args[0]
    assert args[0] == "0xwrap"
    assert args[1] == "0xcanon"
