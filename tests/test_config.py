from src.config_loader import load_chains, load_tokens


def test_load_chains():
    chains = load_chains()
    assert "celo" in chains
    assert "solana" in chains
    assert chains["celo"].hub_stable == "USDT"
    assert chains["solana"].hub_stable == "USDC"


def test_load_tokens():
    tokens = load_tokens()
    assert "VCHF" in tokens
    assert "celo" in tokens["VCHF"].chains
    assert "solana" in tokens["VCHF"].chains
