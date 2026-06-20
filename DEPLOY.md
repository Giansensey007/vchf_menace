# Deploy — VCHF Menace

## Railway

1. Connect Railway to https://github.com/Giansensey007/vchf_menace
2. Root directory: repo root (Dockerfile at root)
3. Mount **persistent volume** at `/data` (SQLite at `/data/bot.db`)
4. Copy all vars from `.env.example` into Railway env
5. **Start with `DRY_RUN=true`** (Dockerfile and `is_dry_run()` default to true)
6. Preflight (Railway shell or one-off job):
   ```bash
   DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all
   DRY_RUN=true python -m pytest tests/ -q
   ```
7. Fund to `config/production.yaml` targets — see `docs/PRODUCTION_STATUS.md`
8. Re-run `verify-all`; optional live probes:
   `DRY_RUN=false python scripts/test_probe_trades.py --execute` (5 VCHF / ~$5 legs)
9. Set `DRY_RUN=false` and run deploy bot: `python -m src.main` (200–2000 VCHF sizing)

## Deploy vs test

| Entry | Purpose | Sizes |
|-------|---------|-------|
| `python -m src.main` | **Deploy** — poll, dynamic sizing, min profit | 200–2000 VCHF |
| `python scripts/test_probe_trades.py` | **Test** — tiny swaps, loss OK | 5 VCHF / ~$5 USDC |
| `python scripts/execute_route_matrix.py --step verify-all` | **Preflight** — claims, sims, funding audit | 31 VCHF quotes |
| `python scripts/rebalance_for_test.py` | **Fund** — move stables for 31 VCHF matrix | per `route_test` in production.yaml |
| `python scripts/convert_platform_chf.py` | **Fund** — CHF→USDC on VNX (min 30 USDC order) | optional |

## Required env vars

| Variable | Notes |
|----------|-------|
| `DRY_RUN` | `true` until funded + verify-all passes |
| `CELO_PRIVATE_KEY` | Celo hot wallet |
| `SOLANA_SECRET_KEY` | Solana hot wallet (base58) |
| `SOLANA_PUBLIC_KEY` | Solana pubkey (withdraw whitelisting) |
| `VNX_PRIVATE_KEY_B64` | VNX platform PEM (base64) |
| `VNX_API_PUBLIC_KEY` | From VNX Platform → My Account |
| `VNX_CELO_WITHDRAW_LABEL` | Whitelisted Celo withdraw label |
| `VNX_SOL_WITHDRAW_LABEL` | Whitelisted Sol withdraw label |
| `VNX_ETH_WITHDRAW_LABEL` | Whitelisted ETH USDC withdraw label |
| `ENABLE_VNX_ARB_ROUTES` | `true` — celo↔vnx VCHF (hub USDT path) |
| `ENABLE_VNX_CCTP_ROUTES` | `true` — SOL↔platform via Circle CCTP |
| `MIN_TRADE_VCHF`, `MAX_TRADE_VCHF` | Deploy sizing: `200` / `2000` |
| `RPC_CELO`, `RPC_SOLANA`, `RPC_ETHEREUM` | Mainnet RPCs — use paid Solana RPC in prod |
| `SOL_RPC_MIN_INTERVAL_MS` | 800+ on public RPC; lower on Helius/QuickNode |
| `DB_PATH` | Docker sets `/data/bot.db` — mount volume at `/data` |

## VNX minimums (enforced in code)

| Guard | Value | Where |
|-------|-------|-------|
| CELO/SOL VCHF deposit credit | 5 VCHF cumulative | `VNX_MIN_DEPOSIT_VCHF_*` |
| ETH USDC deposit credit | 20 USDC cumulative | `VNX_MIN_DEPOSIT_USDC_ETH` |
| Platform buy/sell order | 30 VCHF | `src/vnx/trading.py` |

## VNX API keys

`VNX_API_PUBLIC_KEY` must match **VNX Platform → My Account**. If
`scripts/derive_vnx_public_key.py` gets HTTP 401, copy the public key from the UI.

1. Whitelist Celo, Solana, and ETH hot wallet addresses on VNX
2. Confirm VCHF deposit/withdraw for CELO and SOL; USDC for ETH
3. Optional: top up CHF, then `python scripts/convert_platform_chf.py --execute`

## Local Docker

```bash
docker compose up --build
# DRY_RUN=true is set in docker-compose; override in .env only when going live
```
