from __future__ import annotations

import os

from src.config_loader import ROOT, load_bot_config, load_bridge_config, load_chains, load_tokens
from src.scanner.routes import ALL_DIRECTIONS, ALL_ROUTES


def sanity_check_config() -> tuple[bool, list[str]]:
    issues: list[str] = []
    chains = load_chains()
    tokens = load_tokens()
    cfg = load_bot_config()

    for key in ("celo", "base", "solana", "vnx"):
        if key not in chains:
            issues.append(f"missing chain: {key}")

    if "VCHF" not in tokens:
        issues.append("missing VCHF token")
    else:
        vchf = tokens["VCHF"]
        for ck in ("celo", "base", "solana", "vnx"):
            if ck not in vchf.chains:
                issues.append(f"VCHF missing on {ck}")

    if len(ALL_ROUTES) != 10:
        issues.append(f"expected 10 routes, got {len(ALL_ROUTES)}")
    if len(ALL_DIRECTIONS) != 10:
        issues.append(f"expected 10 directions, got {len(ALL_DIRECTIONS)}")

    expected = {
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
    if set(ALL_DIRECTIONS) != expected:
        issues.append(f"direction set mismatch: {set(ALL_DIRECTIONS) ^ expected}")

    if not cfg.enable_vnx_arb_routes:
        from src.scanner.routes import active_directions

        active = set(active_directions(cfg))
        vnx_cctp = {d for d in active if "vnx" in d and ("solana" in d)}
        if cfg.enable_vnx_cctp_routes:
            if "solana_to_vnx" not in active or "vnx_to_solana" not in active:
                issues.append("CCTP sol↔vnx routes should be active")
        elif vnx_cctp:
            issues.append("vnx routes active unexpectedly")
        if "base_to_vnx" in active or "vnx_to_base" in active:
            issues.append("base↔vnx should stay off unless ENABLE_VNX_ARB_ROUTES")
        if "celo_to_vnx" in active or "vnx_to_celo" in active:
            issues.append("celo↔vnx should stay off unless ENABLE_VNX_ARB_ROUTES")
        if "base_to_solana" not in active or "solana_to_base" not in active:
            issues.append("base↔solana routes must stay active")
        if "celo_to_solana" not in active or "solana_to_celo" not in active:
            issues.append("celo↔solana routes must stay active")

    if cfg.min_trade_vchf <= 0 or cfg.max_trade_vchf <= 0:
        issues.append("trade size bounds invalid")
    if cfg.min_trade_vchf >= cfg.max_trade_vchf:
        issues.append("min_trade_vchf must be < max_trade_vchf")

    bridge = load_bridge_config()
    if bridge.get("hub", {}).get("accounting_stable") not in ("USDC", "USDT"):
        issues.append("bridge hub accounting_stable must be USDC or USDT")
    wh = bridge.get("wormhole", {})
    if not wh.get("base_token_bridge") or not wh.get("solana_usdc"):
        issues.append("wormhole bridge config incomplete")
    if not wh.get("celo_token_bridge") or not wh.get("celo_usdt"):
        issues.append("wormhole celo bridge config incomplete")

    root = ROOT
    gi_path = root / ".gitignore"
    if gi_path.exists() and ".env" not in gi_path.read_text():
        issues.append(".gitignore missing .env")

    env_path = root / ".env"
    if env_path.exists() and "YOUR_BASE" in env_path.read_text():
        issues.append(".env still has placeholders")

    return len(issues) == 0, issues


def run_full_sanity() -> tuple[bool, str]:
    ok, issues = sanity_check_config()
    if not ok:
        return False, "; ".join(issues)
    return True, "config/env/routes OK"
