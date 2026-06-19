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


def test_celo_token_bridge_has_code():
    from src.config_loader import load_chains
    from src.execution.celo import CeloExecutor

    wh = load_bridge_config()["wormhole"]
    bridge = Web3.to_checksum_address(wh["celo_token_bridge"])
    celo = CeloExecutor(load_chains()["celo"])
    code = celo.w3.eth.get_code(bridge)
    assert len(code) > 100, f"Celo Token Bridge {bridge} has no contract code"
