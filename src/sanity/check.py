from __future__ import annotations

import os

from src.config_loader import ROOT, load_bot_config, load_bridge_config, load_chains, load_tokens
from src.scanner.routes import ALL_DIRECTIONS, ALL_ROUTES


def sanity_check_config() -> tuple[bool, list[str]]:
    issues: list[str] = []
    chains = load_chains()
    tokens = load_tokens()
    cfg = load_bot_config()

    for key in ("base", "solana", "ethereum", "vnx"):
        if key not in chains:
            issues.append(f"missing chain: {key}")

    if "VCHF" not in tokens:
        issues.append("missing VCHF token")
    else:
        vchf = tokens["VCHF"]
        for ck in ("base", "solana", "ethereum", "vnx"):
            if ck not in vchf.chains:
                issues.append(f"VCHF missing on {ck}")

    if len(ALL_ROUTES) != 8:
        issues.append(f"expected 8 routes, got {len(ALL_ROUTES)}")
    if len(ALL_DIRECTIONS) != 8:
        issues.append(f"expected 8 directions, got {len(ALL_DIRECTIONS)}")

    expected = {
        "base_to_solana",
        "solana_to_base",
        "base_to_vnx",
        "vnx_to_base",
        "solana_to_vnx",
        "vnx_to_solana",
        "ethereum_to_vnx",
        "vnx_to_ethereum",
    }
    if set(ALL_DIRECTIONS) != expected:
        issues.append(f"direction set mismatch: {set(ALL_DIRECTIONS) ^ expected}")

    if not cfg.enable_vnx_arb_routes:
        from src.scanner.routes import active_directions

        active = set(active_directions(cfg))
        vnx_cctp = {d for d in active if "vnx" in d}
        if cfg.enable_vnx_cctp_routes:
            if "solana_to_vnx" not in active or "vnx_to_solana" not in active:
                issues.append("CCTP sol↔vnx routes should be active")
        elif vnx_cctp:
            issues.append("vnx routes active unexpectedly")
        if "base_to_vnx" in active or "vnx_to_base" in active:
            issues.append("base↔vnx should stay off unless ENABLE_VNX_ARB_ROUTES")
        if "ethereum_to_vnx" in active or "vnx_to_ethereum" in active:
            issues.append("eth↔vnx should stay off unless ENABLE_VNX_ARB_ROUTES")
        if "base_to_solana" not in active or "solana_to_base" not in active:
            issues.append("base↔solana routes must stay active")

    if cfg.min_trade_vchf <= 0 or cfg.max_trade_vchf <= 0:
        issues.append("trade size bounds invalid")
    if cfg.min_trade_vchf >= cfg.max_trade_vchf:
        issues.append("min_trade_vchf must be < max_trade_vchf")

    bridge = load_bridge_config()
    if bridge.get("hub", {}).get("accounting_stable") != "USDC":
        issues.append("bridge hub must use USDC accounting for Base↔Solana")
    wh = bridge.get("wormhole", {})
    if not wh.get("base_token_bridge") or not wh.get("solana_usdc"):
        issues.append("wormhole bridge config incomplete")

    root = ROOT
    gi_path = root / ".gitignore"
    if gi_path.exists() and ".env" not in gi_path.read_text():
        issues.append(".gitignore missing .env")

    env_path = root / ".env"
    if env_path.exists() and "YOUR_BASE" in env_path.read_text():
        issues.append(".env still has placeholders")

    return len(issues) == 0, issues


def sanity_check_env() -> tuple[bool, list[str]]:
    issues: list[str] = []
    for var in ("BASE_PRIVATE_KEY", "SOLANA_SECRET_KEY", "VNX_PRIVATE_KEY_B64"):
        if not os.getenv(var, "").strip():
            issues.append(f"missing {var}")
    if os.getenv("DRY_RUN", "true").lower() not in ("1", "true", "yes"):
        issues.append("DRY_RUN is false — live mode")
    return len(issues) == 0, issues


def run_full_sanity() -> tuple[bool, str]:
    ok_cfg, cfg_issues = sanity_check_config()
    ok_env, env_issues = sanity_check_env()
    all_issues = cfg_issues + env_issues
    evidence = "; ".join(all_issues) if all_issues else "config+env ok (base↔sol active; vnx USDC arb off)"
    return ok_cfg and ok_env, evidence
