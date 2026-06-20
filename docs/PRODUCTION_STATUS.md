# VCHF Menace — Production Status

**Last verified:** 2026-06-20  
**Mode:** `DRY_RUN=true` — live trading paused (funding limits)  
**Template:** GBP Menace · **Bot token:** VCHF

## Test suite

| Metric | Result |
|--------|--------|
| Command | `DRY_RUN=true python -m pytest tests/ -q` |
| Pass / fail | **156 passed**, 0 failed |

## Route matrix (`verify-all`)

Command: `DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all`

### Code checks (critical — all PASS)

| Check | Status | Notes |
|-------|--------|-------|
| `cctp_claim` | PASS | Queue empty; Sol RPC 429 retried with backoff |
| `wormhole_claim` | PASS | Queue empty |
| `wormhole_preflight` | PASS | Celo→Sol/ETH OK; ETH→Celo skipped (ETH USDT under probe) |
| `route_simulations` | PASS | All 6 directions quote @ 31 VCHF |
| `celo_swaps` | PASS | DRY_RUN buy/sell probes |

### Route simulations @ 31 VCHF (quotes only)

| Direction | Active | Net @ 31 VCHF |
|-----------|--------|---------------|
| `celo_to_solana` | yes | ~-$2.05 |
| `solana_to_celo` | yes | ~-$2.06 |
| `celo_to_vnx` | yes | ~-$1.70 |
| `vnx_to_celo` | yes | ~-$0.50 |
| `solana_to_vnx` | yes | ~-$1.67 |
| `vnx_to_solana` | yes | ~-$1.63 |

Negative net at test size is expected (fees + spread); deploy sizing 200–2000 VCHF.

CCTP return path (`cctp_sol_usdc_to_vnx`) implemented in `ArbExecutor.run_cctp_usdc_return_to_vnx` and closed-loop treasury; not a directed route pair.

### On-chain probes (SKIP — funding, not code)

| Probe | Status | Blocker |
|-------|--------|---------|
| `sol_swaps` | SKIP | Sol USDC 0.23 < 0.4 probe min |
| `platform_probe` | SKIP | Platform USDC 11.62 < 42 (31×1.35) |
| `eth_to_vnx` | SKIP | ETH USDC 0.30 < VNX min 20 |
| `vnx_to_eth` | SKIP | Platform USDC 11.62 < 20 |
| `cctp_sol_eth` | SKIP | Sol USDC insufficient |
| `cctp_eth_sol` | SKIP | ETH USDC insufficient |

## Current balances vs targets

### Production (`config/production.yaml`)

| Asset | Have | Target | Gap |
|-------|------|--------|-----|
| VNX VCHF | 1.35 | 200 | +198.65 |
| VNX USDC | 11.62 | 250 | +238.38 |
| Celo USDT | 0.97 | 250 | +249.03 |
| Sol USDC | 0.23 | 250 | +249.77 |
| ETH USDC | 0.30 | 50 | +49.70 |
| ETH USDT | 0.53 | 50 | +49.47 |
| ETH gas | 0.0035 | 0.015 | +0.01 |
| CELO gas | 60.17 | 0.50 | OK |
| SOL gas | 0.44 | 0.05 | OK |

### Route test minimum (31 VCHF matrix)

| Asset | Have | Target | Gap |
|-------|------|--------|-----|
| VNX VCHF | 1.35 | 32 | +30.65 |
| VNX USDC | 11.62 | 45 | +33.38 |
| Celo USDT | 0.97 | 45 | +44.03 |
| Sol USDC | 0.23 | 45 | +44.77 |
| ETH USDC | 0.30 | 3 | +2.70 |
| ETH USDT | 0.53 | 5 | +4.47 |

Use `python scripts/rebalance_for_test.py` after funding to reach route-test mins.

## Production guards

| Guard | Value | Enforced in |
|-------|-------|-------------|
| VCHF deposit min (CELO/SOL) | 5 VCHF cumulative | `src/vnx/deposits.py` |
| ETH USDC deposit min | 20 USDC cumulative | `src/vnx/deposits.py` |
| Platform buy/sell min | 30 VCHF | `src/vnx/trading.py`, VNX `VCHF/USDC` |
| `platform_vchf_only` | true | treasury + executor |
| Solana RPC throttle | 800 ms + 429 backoff | `.env.example`, `src/execution/sol_rpc.py` |
| VNX collision retry | 3 × 5s backoff | `VNX_COLLISION_RETRY_MAX` |
| Docker default | `DRY_RUN=true` | `Dockerfile`, `docker-compose.yml` |

## Implementation coverage

| Area | Status |
|------|--------|
| 6 arb directions in executor | Implemented |
| CCTP Sol→ETH→VNX return | Implemented |
| Wormhole hub legs (Celo↔ETH↔Sol USDT) | Implemented + matrix steps |
| Min deposit guards (5 VCHF, 20 ETH USDC, 30 VCHF order) | Enforced |
| Solana RPC rate limiting | `SOL_RPC_MIN_INTERVAL_MS` + 429 backoff |
| VNX collision handling (shared GBP account) | `VNX_COLLISION_RETRY_MAX=3` |
| In-flight ledger (withdraw/deposit/CCTP/Wormhole) | Same as GBP Menace |
| `run_live_vnx_celo_sol_route.py` | Present |
| `--size` on route matrix | Present (default 31 VCHF) |
| `convert_platform_chf.py` | Present (CHF→USDC, min 30 USDC order) |
| Entry point `src/main.py` | Poll loop, `DRY_RUN` default true |
| Docker `/data` volume | `DB_PATH=/data/bot.db` |
| `.env.example` | Complete (RPCs, rate limits, VNX, CCTP, Wormhole) |
| `.env` local | Present; gitignored |

## Target closed loop (Celo → Sol homing)

**Platform VCHF → CELO withdraw → sell VCHF/USDT → Wormhole USDT to Sol → buy VCHF (Jupiter) → deposit to platform**

| Step | Route leg / script |
|------|-------------------|
| 1 | `vnx_to_celo` — withdraw VCHF, sell on Celo for USDT |
| 2 | `wormhole_celo_to_sol_direct` — USDT Celo → Sol |
| 3 | Jupiter USDC→VCHF (or USDT→USDC→VCHF if needed) |
| 4 | `solana_to_vnx` — deposit VCHF to VNX |

Script: `python scripts/run_live_vnx_celo_sol_route.py`  
Treasury `close_loop_always_return` + `consolidate_vchf_to_platform()` sweep idle on-chain VCHF back to platform after cycles.

## Known blockers before live

1. **Funding** — hot wallet under all production and route-test targets (see tables above)
2. **VNX API** — `queryWithdrawals` / `queryTransfers` return HTTP 403 (in-flight ledger + balance polling still work)
3. **Paid Solana RPC** — public endpoint hits 429 during CCTP discover; set `RPC_SOLANA` to Helius/QuickNode for prod
4. **Jupiter API key** — optional but reduces 429 on route sims (`JUPITER_API_KEY`)
5. **On-chain probes** — re-run `verify-all` after funding; set `DRY_RUN=false` only when critical probes PASS

## Go-live checklist

1. Copy `.env.example` → `.env` (same CELO/SOL keys as GBP; separate VNX keys optional)
2. Whitelist withdraw labels: `VNX_CELO_WITHDRAW_LABEL`, `VNX_SOL_WITHDRAW_LABEL`, `VNX_ETH_WITHDRAW_LABEL`
3. Fund per `config/production.yaml`
4. `DRY_RUN=true python -m pytest tests/ -q` → 156 passed
5. `DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all`
6. Re-run until on-chain probes PASS
7. Deploy to Railway per `DEPLOY.md`; mount `/data` volume
8. Set `DRY_RUN=false` only after critical verify-all checks PASS

## Quick commands

```bash
cd environment/VCHF_Menace
DRY_RUN=true python -m pytest tests/ -q
DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all
python scripts/rebalance_for_test.py   # fund for 31 VCHF matrix
python scripts/run_live_vnx_celo_sol_route.py   # closed-loop live route
python scripts/execute_route_matrix.py --step vnx_to_celo --size 31
```
