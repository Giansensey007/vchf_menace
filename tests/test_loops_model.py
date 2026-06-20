"""Platform-first loop model: same-asset round trips (Loop 1/2/3)."""

from __future__ import annotations

from src.config_loader import TokenConfig
from src.scanner.routes import (
    LOOP1_OUTBOUND,
    LOOP2_INBOUND,
    LOOP3_CROSS,
    LoopSpec,
    StepKind,
    active_loops,
    bridge_mechanism,
)

TOKEN = TokenConfig(
    symbol="VCHF",
    decimals=18,
    chains={"celo": "0xc", "base": "0xb", "solana": "solV", "vnx": "VCHF"},
)


def _loops():
    return active_loops(token=TOKEN)


def _by_family(family: str) -> list[LoopSpec]:
    return [loop for loop in _loops() if loop.family == family]


def test_loop_counts_for_vchf():
    loops = _loops()
    assert len(loops) == 12  # L1=3, L2=3, L3=6 for {celo, base, solana}
    assert len(_by_family(LOOP1_OUTBOUND)) == 3
    assert len(_by_family(LOOP2_INBOUND)) == 3
    assert len(_by_family(LOOP3_CROSS)) == 6


def test_loop_keys_unique():
    keys = [loop.key for loop in _loops()]
    assert len(keys) == len(set(keys))


def test_bridge_mechanism_cctp_first():
    assert bridge_mechanism("base", "ethereum") == "cctp"
    assert bridge_mechanism("solana", "ethereum") == "cctp"
    assert bridge_mechanism("base", "solana") == "cctp"
    assert bridge_mechanism("celo", "ethereum") == "wormhole"
    assert bridge_mechanism("celo", "solana") == "wormhole"
    # Celo (USDT) <-> Base (USDC) has no native direct bridge => ETH triangle
    assert bridge_mechanism("celo", "base") == "eth_triangle"


def test_loop1_base_uses_cctp_to_hub():
    loop = LoopSpec(LOOP1_OUTBOUND, "VCHF", "base")
    assert loop.bridge_legs[0].bridge_to == "ethereum"
    assert loop.bridge_legs[0].mechanism == "cctp"


def test_loop3_base_to_solana_cctp_and_celo_base_triangle():
    base_sol = LoopSpec(LOOP3_CROSS, "VCHF", "base", "solana")
    assert base_sol.bridge_legs[0].mechanism == "cctp"
    celo_base = LoopSpec(LOOP3_CROSS, "VCHF", "celo", "base")
    assert celo_base.bridge_legs[0].mechanism == "eth_triangle"


def test_every_loop_is_same_asset_round_trip():
    for loop in _loops():
        steps = loop.steps()
        assert steps[-1].kind in (StepKind.PLATFORM_BUYBACK, StepKind.VNX_TOKEN_DEPOSIT)
        buybacks = [s for s in steps if s.is_buyback]
        assert len(buybacks) == 1
        for s in steps:
            if s.kind in (StepKind.PLATFORM_BUYBACK, StepKind.ONCHAIN_BUYBACK):
                assert s.is_buyback
