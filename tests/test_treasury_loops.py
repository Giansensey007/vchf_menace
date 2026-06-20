from src.treasury.loops import (
    INVERSE_DIRECTION,
    LEG_END_STABLE,
    inverse_direction,
    return_closes_origin,
    return_closes_origin_with_cctp,
    return_leg_direction,
    use_cctp_usdc_return,
)


def test_inverse_pairs():
    for a, b in INVERSE_DIRECTION.items():
        assert INVERSE_DIRECTION[b] == a


def test_base_round_trip_closes():
    assert return_closes_origin("base", "base_to_vnx")
    assert inverse_direction("base_to_vnx") == "vnx_to_base"


def test_base_sol_round_trip_closes_via_inverse():
    # base_to_solana ends on Sol USDC; solana_to_base returns to Base USDT
    assert return_closes_origin("base", "base_to_solana")
    assert inverse_direction("base_to_solana") == "solana_to_base"


def test_vnx_to_sol_uses_cctp_return():
    assert use_cctp_usdc_return("vnx", "vnx_to_solana")
    assert return_leg_direction("vnx", "vnx_to_solana") == "cctp_sol_usdc_to_vnx"
    assert return_closes_origin_with_cctp("vnx", "vnx_to_solana")
    assert return_closes_origin("vnx", "vnx_to_solana")  # legacy VCHF inverse also closes
