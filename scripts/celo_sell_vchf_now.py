#!/usr/bin/env python3
"""One-shot: sell all Celo VCHF for USDT (no bridge imports)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.config_loader import load_bot_config, load_chains, load_tokens, token_decimals
from src.execution.celo import CeloExecutor
from src.execution.evm_swap import validate_swap_min_out
from src.quotes.types import to_human

chains = load_chains()
token = load_tokens()["VCHF"]
cfg = load_bot_config()
celo = CeloExecutor(chains["celo"])
dec = token_decimals(token, "celo")
usdt_token = chains["celo"].hub_token
vchf_raw = celo.balance_erc20(token.chains["celo"])
vchf_ui = float(to_human(vchf_raw, dec))
usdt_before = float(to_human(celo.balance_erc20(usdt_token), chains["celo"].hub_decimals))
print(f"Celo VCHF={vchf_ui:.4f} USDT={usdt_before:.2f}")
if vchf_raw <= 0:
    print("Nothing to sell")
    sys.exit(0)
sim = celo.simulate_swap(token.chains["celo"], usdt_token, vchf_raw, cfg.slippage_bps)
if not sim:
    print("FAIL: no quote")
    sys.exit(1)
min_usdt = int(sim["amount_out"] * (1 - cfg.slippage_bps / 10000))
guard_err = validate_swap_min_out(min_usdt, label="celo sell VCHF")
if guard_err:
    print(f"FAIL: {guard_err}")
    sys.exit(1)
tx = celo.swap_exact_input(token.chains["celo"], usdt_token, vchf_raw, min_usdt)
if not tx:
    print("FAIL: swap broadcast")
    sys.exit(1)
usdt_after = float(to_human(celo.balance_erc20(usdt_token), chains["celo"].hub_decimals))
print(f"OK tx={tx}")
print(f"USDT after={usdt_after:.2f}")
