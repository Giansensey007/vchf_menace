from src.config_loader import load_chains, load_tokens


def test_load_chains():
    chains = load_chains()
    assert "base" in chains
    assert chains["base"].hub_stable == "USDC"
    assert chains["base"].kyber_slug == "base"
    assert chains["base"].rpc_env == "RPC_BASE"


def test_load_tokens():
    tokens = load_tokens()
    assert "VCHF" in tokens
    assert "base" in tokens["VCHF"].chains
    assert "solana" in tokens["VCHF"].chains
