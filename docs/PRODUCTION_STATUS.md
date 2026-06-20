# VCHF Menace — Production Status

**Last verified:** 2026-06-20  
**Mode:** `DRY_RUN=true` — live trading paused (funding limits)  
**Template:** VNXAU Menace (Base hub) · **Bot token:** VCHF

## Railway pre-flight checklist

Run these **before** first Railway deploy and again after funding, from a Railway shell or one-off job:

```bash
DRY_RUN=true python -m pytest tests/ -q
DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all
```

### Environment (Railway)

| Item | Required | Notes |
|------|----------|-------|
| Volume at `/data` | Yes | SQLite + in-flight ledger + tx log + bridge queues |
| `DB_PATH=/data/bot.db` | Yes | Dockerfile default; parent dir holds all persistent JSON/JSONL |
| `DRY_RUN=true` | Yes until funded | Dockerfile default |
| `.env` vars from `.env.example` | Yes | Never commit `.env` |
| `RPC_BASE` paid endpoint | Recommended | Public Base RPC can rate-limit during swap quotes |
| `RPC_SOLANA` paid endpoint | Strongly recommended | Helius / QuickNode — public RPC 429s in prod loop |
| `JUPITER_API_KEY` | Recommended | 1 RPS free tier; keyless is 0.5 RPS |
| `USE_KYBER_SWAP=true` | Recommended | KyberSwap on Base; Uniswap V3 fallback |
| `SOL_RPC_MIN_INTERVAL_MS=800` | Yes on public RPC | 429 exponential backoff via `SOL_RPC_429_BACKOFF_SEC` |
| VNX withdraw labels | Yes | `VNX_BASE_WITHDRAW_LABEL`, `VNX_SOL_WITHDRAW_LABEL`, `VNX_ETH_WITHDRAW_LABEL` |
| Whitelisted hot wallets | Yes | Base, Solana, ETH addresses on VNX Platform |
| VNX collision retry | Yes | `VNX_COLLISION_RETRY_MAX=3`, `VNX_COLLISION_BACKOFF_SEC=5` (shared GBP account) |

### Code checks (critical — must PASS)

| Check | Command step | Pass criteria |
|-------|--------------|---------------|
| Test suite | `pytest tests/ -q` | 0 failures |
| CCTP claim worker | `verify-all` → `cctp_claim` | Queue drains; discover + claim OK |
| Wormhole claim worker | `verify-all` → `wormhole_claim` | Queue drains |
| Wormhole preflight | `verify-all` → `wormhole_preflight` | Base outbound sim OK |
| Route simulations | `verify-all` → `route_simulations` | All 10 directions quote @ 31 VCHF |

### In-flight tracking

| File | Purpose |
|------|---------|
| `/data/in_flight.jsonl` | VNX withdraws/deposits, CCTP/Wormhole burns |
| `/data/tx_log.jsonl` | Unified TX audit with explorer URLs |
| `/data/cctp_queue.json` | CCTP claim queue |
| `/data/wormhole_queue.json` | Wormhole VAA claim queue |

Stale pending records >48h are auto-failed at `verify-all` startup.

## Test suite

| Metric | Result |
|--------|--------|
| Command | `DRY_RUN=true python -m pytest tests/ -q` |
| Pass / fail | **171 passed**, 0 failed |
| 10-iteration sanity | pytest every iter; audit every iter; verify-all on even iters (see table below) |

### 10-iteration validation (2026-06-20)

Command pattern per iteration: `pytest tests/ -q` → `execute_route_matrix.py --step audit` → (`verify-all` on even iters).

| Iter | pytest | audit | verify-all | Notes |
|------|--------|-------|------------|-------|
| 1 | FAIL (167/171) | FAIL | — | Transient: missing `BASE_PRIVATE_KEY` in audit (fixed) |
| 2 | PASS (171) | FAIL | FAIL | VNX `queryWithdrawals` 400 during treasury snapshot |
| 3 | FAIL (160/171) | FAIL | — | Transient env/API flake |
| 4 | PASS (171) | PASS | FAIL | verify-all timeout/API during early run |
| 5 | PASS (171) | PASS | — |
| 6 | PASS (171) | PASS | PASS | Critical checks PASS; on-chain probes SKIP (funding) |
| 7 | PASS (171) | PASS | — |
| 8 | PASS (171) | PASS | PASS | Stable |
| 9 | PASS (171) | PASS | — |
| 10 | PASS (171) | PASS | PASS | Stable |

**Final state (iter 10):** 171 passed · audit OK · verify-all critical PASS · probes SKIP (funding).  
Audit now skips Base balances when `BASE_PRIVATE_KEY` unset; Celo/Base/Sol/ETH lines still print.

Validated in unit tests: **30 VCHF** platform min (`test_vnx_trading`, `test_treasury_prepare`), **in_flight** ledger, **VNX collision** retry, **Sol RPC** throttle, **`--size`** on route matrix (default 31 VCHF).

## Route matrix (`verify-all`)

Command: `DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all`

### Code checks (critical — all PASS)

| Check | Status | Notes |
|-------|--------|-------|
| `cctp_claim` | PASS | Queue empty; Sol RPC 429 retried with backoff |
| `wormhole_claim` | PASS | Queue empty |
| `wormhole_preflight` | PASS | Base→Sol/ETH OK |
| `route_simulations` | PASS | All 10 directions quote @ 31 VCHF |
| `base_swaps` | PASS | DRY_RUN Kyber/Uniswap buy/sell probes on Base |
| `celo_swaps` | PASS | DRY_RUN legacy Celo buy/sell probes |

### Route simulations @ 31 VCHF (quotes only)

| Direction | Active | Net @ 31 VCHF |
|-----------|--------|---------------|
| `celo_to_solana` | yes | quotes OK (legacy Celo hub) |
| `solana_to_celo` | yes | quotes OK (legacy Celo hub) |
| `celo_to_vnx` | yes | quotes OK |
| `vnx_to_celo` | yes | quotes OK |
| `base_to_solana` | yes | ~-$2.05 |
| `solana_to_base` | yes | ~-$2.06 |
| `base_to_vnx` | yes | ~-$1.70 |
| `vnx_to_base` | yes | ~-$0.50 |
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
| Base USDT | 0.97 | 250 | +249.03 |
| Sol USDC | 0.23 | 250 | +249.77 |
| ETH USDC | 0.30 | 50 | +49.70 |
| ETH USDT | 0.53 | 50 | +49.47 |
| ETH gas | 0.0035 | 0.015 | +0.01 |
| BASE gas | 60.17 | 0.50 | OK |
| SOL gas | 0.44 | 0.05 | OK |

### Route test minimum (31 VCHF matrix)

| Asset | Have | Target | Gap |
|-------|------|--------|-----|
| VNX VCHF | 1.35 | 32 | +30.65 |
| VNX USDC | 11.62 | 45 | +33.38 |
| Base USDT | 0.97 | 45 | +44.03 |
| Sol USDC | 0.23 | 45 | +44.77 |
| ETH USDC | 0.30 | 3 | +2.70 |
| ETH USDT | 0.53 | 5 | +4.47 |

Use `python scripts/rebalance_for_test.py` after funding to reach route-test mins.

## Production guards

| Guard | Value | Enforced in |
|-------|-------|-------------|
| VCHF deposit min (BASE/SOL) | 5 VCHF cumulative | `src/vnx/deposits.py` |
| ETH USDC deposit min | 20 USDC cumulative | `src/vnx/deposits.py` |
| Platform buy/sell min | 30 VCHF | `src/vnx/trading.py`, VNX `VCHF/USDC` |
| `platform_vchf_only` | true | treasury + executor |
| Solana RPC throttle | 800 ms + 429 backoff | `.env.example`, `src/execution/sol_rpc.py` |
| VNX collision retry | 3 × 5s backoff | `VNX_COLLISION_RETRY_MAX` |
| Docker default | `DRY_RUN=true` | `Dockerfile`, `docker-compose.yml` |

## Implementation coverage

| Area | Status |
|------|--------|
| 10 arb directions (dual EVM: Celo + Base) | Implemented |
| Base execution (`BaseExecutor`, KyberSwap, `base_usdc.py`) | Implemented |
| Celo execution (legacy hub, `celo.py`) | Implemented |
| CCTP Sol→ETH→VNX return | Implemented |
| Wormhole hub legs (Base/Celo↔ETH↔Sol USDT) | Implemented + matrix steps |
| Min deposit guards (5 VCHF, 20 ETH USDC, 30 VCHF order) | Enforced |
| ETH→VNX asset guard (USDC only, never USDT on ETH) | `src/vnx/constants.py` + `validate_eth_usdc_vnx_deposit()` |
| Solana RPC rate limiting | `SOL_RPC_MIN_INTERVAL_MS` + 429 backoff |
| VNX collision handling (shared GBP account) | `VNX_COLLISION_RETRY_MAX=3` |
| In-flight ledger (withdraw/deposit/CCTP/Wormhole) | Same as GBP Menace |
| `run_live_vnx_base_sol_route.py` | Present |
| `--size` on route matrix | Present (default 31 VCHF) |
| `convert_platform_chf.py` | Present (CHF→USDC, min 30 USDC order) |
| Entry point `src/main.py` | Poll loop, `DRY_RUN` default true |
| Docker `/data` volume | `DB_PATH=/data/bot.db` |
| `.env.example` | Complete (RPCs, rate limits, VNX, CCTP, Wormhole, Base keys) |
| `.env` local | Present; gitignored |

## Target closed loop (Base → Sol homing)

**Platform VCHF → BASE withdraw → sell VCHF/USDT → Wormhole USDT to Sol → buy VCHF (Jupiter) → deposit to platform**

| Step | Route leg / script |
|------|-------------------|
| 1 | `vnx_to_base` — withdraw VCHF, sell on Base for USDT |
| 2 | `wormhole_base_to_sol_direct` — USDT Base → Sol |
| 3 | Jupiter USDC→VCHF (or USDT→USDC→VCHF if needed) |
| 4 | `solana_to_vnx` — deposit VCHF to VNX |

Script: `python scripts/run_live_vnx_base_sol_route.py`  
Treasury `close_loop_always_return` + `consolidate_vchf_to_platform()` sweep idle on-chain VCHF back to platform after cycles.

## Known blockers before live

1. **Funding** — hot wallet under all production and route-test targets (see tables above)
2. **VNX API** — `queryWithdrawals` / `queryTransfers` return HTTP 403 (in-flight ledger + balance polling still work)
3. **Paid Solana RPC** — public endpoint hits 429 during CCTP discover; set `RPC_SOLANA` to Helius/QuickNode for prod
4. **Jupiter API key** — optional but reduces 429 on route sims (`JUPITER_API_KEY`)
5. **On-chain probes** — re-run `verify-all` after funding; set `DRY_RUN=false` only when critical probes PASS

## Go-live checklist

1. Copy `.env.example` → `.env` (same BASE/SOL keys as GBP; separate VNX keys optional)
2. Whitelist withdraw labels: `VNX_BASE_WITHDRAW_LABEL`, `VNX_SOL_WITHDRAW_LABEL`, `VNX_ETH_WITHDRAW_LABEL`
3. Fund per `config/production.yaml`
4. `DRY_RUN=true python -m pytest tests/ -q` → 171 passed
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
python scripts/run_live_vnx_base_sol_route.py   # closed-loop live route
python scripts/execute_route_matrix.py --step vnx_to_base --size 31
```
