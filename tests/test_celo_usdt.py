from unittest.mock import MagicMock, patch

from src.bridge.celo_usdt import celo_usdt_addresses, consolidate_wrapped_to_canonical


def test_celo_usdt_addresses_match_config():
    canonical, wrapped = celo_usdt_addresses()
    assert canonical.lower() == "0x48065fbbe25f71c9282ddf5e1cd6d6a887483d5e"
    assert wrapped.lower() == "0x617f3112bf5397d0467d315cc709ef968d9ba546"
    assert canonical != wrapped


def test_consolidate_skips_when_no_wrapped():
    celo = MagicMock()
    celo.balance_erc20.return_value = 0
    r = consolidate_wrapped_to_canonical(celo=celo)
    assert r["success"] is True
    assert r["skipped"] is True


def test_consolidate_swaps_wrapped_to_canonical():
    celo = MagicMock()
    celo.balance_erc20.return_value = 5_000_000
    celo.simulate_swap.return_value = {"amount_out": 4_990_000}
    celo.swap_exact_input.return_value = "0xabc"
    with patch("src.bridge.celo_usdt.celo_usdt_addresses", return_value=("0xcanon", "0xwrap")):
        r = consolidate_wrapped_to_canonical(5.0, celo=celo)
    assert r["success"] is True
    assert r["tx"] == "0xabc"
    celo.swap_exact_input.assert_called_once()
    args = celo.swap_exact_input.call_args[0]
    assert args[0] == "0xwrap"
    assert args[1] == "0xcanon"
