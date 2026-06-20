"""Wormhole bridge contract sanity."""
from web3 import Web3

from src.config_loader import load_bridge_config


def test_ethereum_token_bridge_has_code():
    from src.config_loader import load_chains
    from src.execution.ethereum import EthereumExecutor

    wh = load_bridge_config()["wormhole"]
    bridge = Web3.to_checksum_address(wh["ethereum_token_bridge"])
    eth = EthereumExecutor(load_chains()["ethereum"])
    code = eth.w3.eth.get_code(bridge)
    assert len(code) > 100, f"ETH Token Bridge {bridge} has no contract code"


def test_base_token_bridge_has_code():
    from src.config_loader import load_chains
    from src.execution.base import BaseExecutor

    wh = load_bridge_config()["wormhole"]
    bridge = Web3.to_checksum_address(wh["base_token_bridge"])
    base = BaseExecutor(load_chains()["base"])
    code = base.w3.eth.get_code(bridge)
    assert len(code) > 100, f"Base Token Bridge {bridge} has no contract code"
