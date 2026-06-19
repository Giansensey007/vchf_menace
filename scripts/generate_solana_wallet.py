#!/usr/bin/env python3
"""Generate a new Solana hot wallet for VCHF Menace."""
from __future__ import annotations

import json
import sys

from solders.keypair import Keypair


def main() -> None:
    kp = Keypair()
    secret_b58 = str(kp)
    secret_arr = list(bytes(kp))
    pubkey = str(kp.pubkey())
    print("=== Solana Hot Wallet (save to .env, never commit) ===")
    print(f"Public key:  {pubkey}")
    print(f"Secret (base58 for SOLANA_SECRET_KEY): {secret_b58}")
    print(f"Secret (JSON array): {json.dumps(secret_arr)}")
    print("\nAdd to .env:")
    print(f"SOLANA_SECRET_KEY={secret_b58}")


if __name__ == "__main__":
    main()
