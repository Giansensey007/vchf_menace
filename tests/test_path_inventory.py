"""Golden live path inventory: directed keys, loop keys, bridge matrix."""

from __future__ import annotations

from src.config_loader import load_tokens
from src.scanner.routes import (
    ALL_DIRECTIONS,
    LOOP1_OUTBOUND,
    LoopSpec,
    active_loops,
    bridge_mechanism,
)

EXPECTED_DIRECTIONS = frozenset(
    {
        "celo_to_solana",
        "solana_to_celo",
        "celo_to_vnx",
        "vnx_to_celo",
        "base_to_solana",
        "solana_to_base",
        "base_to_vnx",
        "vnx_to_base",
        "solana_to_vnx",
        "vnx_to_solana",
    }
)

EXPECTED_LOOPS = frozenset(
    {
        "loop1_outbound:celo",
        "loop1_outbound:base",
        "loop1_outbound:solana",
        "loop2_inbound:celo",
        "loop2_inbound:base",
        "loop2_inbound:solana",
        "loop3_cross:celo->base",
        "loop3_cross:celo->solana",
        "loop3_cross:base->celo",
        "loop3_cross:base->solana",
        "loop3_cross:solana->celo",
        "loop3_cross:solana->base",
    }
)


def test_live_directed_inventory():
    assert frozenset(ALL_DIRECTIONS) == EXPECTED_DIRECTIONS
    assert "vnx_to_base" in ALL_DIRECTIONS
    assert len(ALL_DIRECTIONS) == 10


def test_live_loop_inventory():
    token = load_tokens()["VCHF"]
    keys = frozenset(loop.key for loop in active_loops(token=token))
    assert keys == EXPECTED_LOOPS
    assert "loop1_outbound:base" in keys
    assert "loop3_cross:celo->base" in keys
    assert len(keys) == 12


def test_bridge_mechanism_matrix():
    assert bridge_mechanism("base", "solana") == "cctp"
    assert bridge_mechanism("solana", "ethereum") == "cctp"
    assert bridge_mechanism("ethereum", "base") == "cctp"
    assert bridge_mechanism("celo", "solana") == "wormhole"
    assert bridge_mechanism("celo", "ethereum") == "wormhole"
    assert bridge_mechanism("celo", "base") == "eth_triangle"
    assert bridge_mechanism("base", "celo") == "eth_triangle"
    assert bridge_mechanism("celo", "celo") == "none"


def test_required_live_keys_present():
    token = load_tokens()["VCHF"]
    keys = {loop.key for loop in active_loops(token=token)}
    assert LoopSpec(LOOP1_OUTBOUND, "VCHF", "base").key in keys
