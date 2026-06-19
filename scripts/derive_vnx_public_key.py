#!/usr/bin/env python3
"""Derive VNX API public key from PEM in .env."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.vnx.auth import derive_public_key_b64


def main() -> None:
    pub = derive_public_key_b64()
    print(f"VNX_API_PUBLIC_KEY={pub}")


if __name__ == "__main__":
    main()
