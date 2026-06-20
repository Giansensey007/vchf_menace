from src.config_loader import load_chains, load_tokens


def test_load_chains():
    chains = load_chains()
    assert "base" in chains
    assert "solana" in chains
    assert chains["base"].hub_stable == "USDC"
    assert chains["solana"].hub_stable == "USDC"


def test_load_tokens():
    tokens = load_tokens()
    assert "VCHF" in tokens
    assert "base" in tokens["VCHF"].chains
    assert "solana" in tokens["VCHF"].chains


def test_base_kyber_slug():
    chains = load_chains()
    assert chains["base"].kyber_slug == "base"
    assert chains["base"].rpc_env == "RPC_BASE"
