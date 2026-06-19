import json
import os

from solders.keypair import Keypair


def test_keypair_from_base58():
    kp = Keypair()
    os.environ["SOLANA_SECRET_KEY"] = str(kp)
    from src.execution.solana import load_keypair

    loaded = load_keypair()
    assert str(loaded.pubkey()) == str(kp.pubkey())


def test_keypair_roundtrip():
    kp = Keypair()
    arr = list(bytes(kp))
    os.environ["SOLANA_SECRET_KEY"] = json.dumps(arr)
    from src.execution.solana import load_keypair

    assert str(load_keypair().pubkey()) == str(kp.pubkey())
