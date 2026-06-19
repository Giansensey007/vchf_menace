from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from spl.token.instructions import get_associated_token_address
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

CCTP_DIR = Path(__file__).resolve().parents[2] / "scripts" / "cctp"


def sol_usdc_ata(owner_pubkey: str, usdc_mint: str) -> str:
    owner = Pubkey.from_string(owner_pubkey)
    mint = Pubkey.from_string(usdc_mint)
    return str(get_associated_token_address(owner, mint))


def _write_anchor_wallet(secret: str) -> Path:
    if secret.startswith("["):
        data = json.loads(secret)
    else:
        import base58

        data = list(base58.b58decode(secret))
    fd, path = tempfile.mkstemp(suffix=".json", prefix="sol-anchor-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return Path(path)


def _cctp_env(
    *,
    sol_rpc: str,
    sol_secret: str,
    sol_usdc_ata_addr: str,
    sol_usdc_mint: str,
    eth_domain: int,
    eth_address: str,
    eth_usdc: str,
    iris_api: str,
) -> dict[str, str]:
    wallet_path = _write_anchor_wallet(sol_secret)
    return {
        "ANCHOR_PROVIDER_URL": sol_rpc,
        "ANCHOR_WALLET": str(wallet_path),
        "USER_TOKEN_ACCOUNT": sol_usdc_ata_addr,
        "SOLANA_USDC_ADDRESS": sol_usdc_mint,
        "REMOTE_EVM_DOMAIN": str(eth_domain),
        "REMOTE_EVM_ADDRESS": eth_address,
        "REMOTE_TOKEN_HEX": eth_usdc,
        "IRIS_API_URL": iris_api,
    }


def _parse_ops_output(stdout: str, stderr: str, returncode: int) -> tuple[str | None, str | None]:
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if returncode != 0:
        return None, err or out or f"exit {returncode}"
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            if data.get("ok"):
                return data.get("tx"), None
            return None, data.get("error")
    return None, out or err or "no output"


def run_burn_sol(
    *,
    amount_raw: int,
    max_fee_raw: int,
    min_finality_threshold: int,
    sol_rpc: str,
    sol_secret: str,
    sol_owner: str,
    sol_usdc_mint: str,
    eth_domain: int,
    eth_address: str,
    eth_usdc: str,
    iris_api: str,
) -> tuple[str | None, str | None]:
    if not CCTP_DIR.joinpath("node_modules").is_dir():
        return None, f"CCTP deps missing — run: cd {CCTP_DIR} && npm install"

    ata = sol_usdc_ata(sol_owner, sol_usdc_mint)
    env = os.environ.copy()
    env.update(
        _cctp_env(
            sol_rpc=sol_rpc,
            sol_secret=sol_secret,
            sol_usdc_ata_addr=ata,
            sol_usdc_mint=sol_usdc_mint,
            eth_domain=eth_domain,
            eth_address=eth_address,
            eth_usdc=eth_usdc,
            iris_api=iris_api,
        )
    )
    cmd = [
        "npm",
        "run",
        "ops",
        "--",
        "burn-sol",
        "--amount",
        str(amount_raw),
        "--maxFee",
        str(max_fee_raw),
        "--minFinalityThreshold",
        str(min_finality_threshold),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=CCTP_DIR, env=env, capture_output=True, text=True, timeout=300, check=False
        )
        if proc.returncode != 0:
            logger.error("CCTP burn-sol failed: %s %s", proc.stdout, proc.stderr)
        return _parse_ops_output(proc.stdout, proc.stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        return None, "CCTP burn-sol timed out"
    except Exception as exc:
        return None, str(exc)


def run_receive_sol(
    *,
    message_hex: str,
    attestation_hex: str,
    sol_rpc: str,
    sol_secret: str,
    sol_owner: str,
    sol_usdc_mint: str,
    eth_domain: int,
    eth_usdc: str,
    iris_api: str,
) -> tuple[str | None, str | None]:
    if not CCTP_DIR.joinpath("node_modules").is_dir():
        return None, f"CCTP deps missing — run: cd {CCTP_DIR} && npm install"

    ata = sol_usdc_ata(sol_owner, sol_usdc_mint)
    env = os.environ.copy()
    env.update(
        _cctp_env(
            sol_rpc=sol_rpc,
            sol_secret=sol_secret,
            sol_usdc_ata_addr=ata,
            sol_usdc_mint=sol_usdc_mint,
            eth_domain=eth_domain,
            eth_address="0x0000000000000000000000000000000000000000",
            eth_usdc=eth_usdc,
            iris_api=iris_api,
        )
    )
    cmd = [
        "npm",
        "run",
        "ops",
        "--",
        "receive-sol",
        "--message",
        message_hex,
        "--attestation",
        attestation_hex,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=CCTP_DIR, env=env, capture_output=True, text=True, timeout=300, check=False
        )
        if proc.returncode != 0:
            logger.error("CCTP receive-sol failed: %s %s", proc.stdout, proc.stderr)
        return _parse_ops_output(proc.stdout, proc.stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        return None, "CCTP receive-sol timed out"
    except Exception as exc:
        return None, str(exc)
