# VCHF Menace

Base ↔ Solana **VCHF/USDC arbitrage executor** with VNX Platform bridging for rebalancing.

- **Repo:** https://github.com/Giansensey007/vchf_menace
- **Local path:** `environment/VCHF_Menace/` (nested git — not VNX_trakcer)
- **Default mode:** `DRY_RUN=true` (simulate only)

## Arb routes (6 directions)

| Route | Buy VCHF on | Sell VCHF on | Rebalance |
|-------|-------------|--------------|-----------|
| `base_to_solana` | Base (USDT) | Solana (USDC) | VNX bridge |
| `solana_to_base` | Solana (USDC) | Base (USDT) | VNX bridge |
| `base_to_vnx` | Base (USDT) | VNX Platform | deposit |
| `vnx_to_base` | VNX Platform | Base (USDT) | withdraw |
| `solana_to_vnx` | Solana (USDC) | VNX Platform | deposit |
| `vnx_to_solana` | VNX Platform | Solana (USDC) | withdraw |

**Coverage:** SOL ↔ BASE, VNX ↔ BASE, VNX ↔ SOL

## Validation (20 agents × 5 iterations)

- **SA-00** `sanity-check-all` — runs every iteration first (config, env, 6 routes, peg checks)
- **SA-01..SA-19** — quotes, routes, wallets, executor, security, DB

```bash
python scripts/run_validation_matrix.py --iterations 5 --live
python scripts/simulate_cycle.py --all --size 50
```

## Quick start

```bash
cd environment/VCHF_Menace
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill secrets
python scripts/generate_solana_wallet.py
python scripts/derive_vnx_public_key.py
pytest tests/ -q
python -m src.main once
```

## Push

Always push from the nested repo:

```bash
cd environment/VCHF_Menace
git add -A && git commit -m "your message" && git push origin main
```

Never commit `.env`.
