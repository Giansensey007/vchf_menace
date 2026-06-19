# VCHF Menace — production status

**Template:** GBP Menace @ `f99144c` · **Bot token:** VCHF · **Default:** `DRY_RUN=true`

## Route matrix (`verify-all`)

Command: `DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all`

| Step | Status | Notes |
|------|--------|-------|
| `cctp_claim` | PASS | Discover + claim (dry-run skips broadcast) |
| `wormhole_claim` | PASS | Queue empty |
| `wormhole_preflight` | PASS | Celo→Sol, Celo→ETH outbound |
| `route_simulations` | PASS | All 6 directions quote @ 31 VCHF |
| `celo_swaps` | PASS | DRY_RUN buy/sell probes |
| `sol_swaps` | SKIP | Sol USDC below probe threshold |
| `platform_probe` | SKIP | Platform USDC below probe threshold |
| `eth_to_vnx` | SKIP | ETH USDC below VNX min 20 |
| `vnx_to_eth` | SKIP | Platform USDC below 20 |
| `cctp_sol_eth` / `cctp_eth_sol` | SKIP | Insufficient hub USDC |

### Route simulations @ 31 VCHF (quotes only)

| Direction | Active | Net @ 31 VCHF |
|-----------|--------|---------------|
| `celo_to_solana` | yes | ~-$2.05 |
| `solana_to_celo` | yes | ~-$2.05 |
| `celo_to_vnx` | yes | ~-$1.70 |
| `vnx_to_celo` | yes | ~-$0.51 |
| `solana_to_vnx` | yes | ~-$1.67 |
| `vnx_to_solana` | yes | ~-$1.63 |

Negative net at test size is expected (fees + spread); deploy sizing 200–2000 VCHF.

### Production guards

| Guard | Value | Enforced in |
|-------|-------|-------------|
| VCHF deposit min (CELO/SOL) | 5 VCHF cumulative | `src/vnx/deposits.py` |
| ETH USDC deposit min | 20 USDC cumulative | `src/vnx/deposits.py` |
| Platform buy/sell min | 30 VCHF | `src/vnx/trading.py`, VNX `VCHF/USDC` |
| `platform_vchf_only` | true | treasury + executor |
| Solana RPC throttle | 800 ms | `.env.example` |
| Docker default | `DRY_RUN=true` | `Dockerfile`, `docker-compose.yml` |

### Route test minimum (31 VCHF matrix)

See `config/production.yaml` → `route_test` (platform_vchf: 32, stables ~45 USDC/USDT).

### Funding gaps (live wallet snapshot)

Current hot wallet is **under-funded** for production and route-test thresholds. Top up before live:

- Platform VCHF ≥ 200 (deploy) / 32 (route test)
- Platform USDC ≥ 250 / 45
- Celo USDT ≥ 250 / 45
- Sol USDC ≥ 250 / 45
- ETH USDC ≥ 50 / 3 (VNX credit min 20 for hub legs)

## Target closed loop (Celo → Sol homing)

**Platform VCHF → CELO withdraw → sell VCHF/USDT → Wormhole USDT to Sol → buy VCHF (Jupiter) → deposit to platform**

| Step | Route leg / script |
|------|-------------------|
| 1 | `vnx_to_celo` — withdraw VCHF, sell on Celo for USDT |
| 2 | `wormhole_celo_to_sol_direct` — USDT Celo → Sol |
| 3 | Jupiter USDC→VCHF (or USDT→USDC→VCHF if needed) |
| 4 | `solana_to_vnx` — deposit VCHF to VNX |

Treasury `close_loop_always_return` + `consolidate_vchf_to_platform()` sweep idle on-chain VCHF back to platform after cycles.

## Go-live checklist

1. Copy `.env.example` → `.env` (same CELO/SOL keys as GBP; separate VNX keys optional)
2. Whitelist withdraw labels: `VNX_CELO_WITHDRAW_LABEL`, `VNX_SOL_WITHDRAW_LABEL`, `VNX_ETH_WITHDRAW_LABEL`
3. Fund per `config/production.yaml`
4. `DRY_RUN=true python -m pytest tests/ -q` → 140 passed
5. `python scripts/execute_route_matrix.py --step verify-all`
6. Re-run until on-chain probes PASS
7. Set `DRY_RUN=false` only after critical verify-all checks PASS

## Quick commands

```bash
cd environment/VCHF_Menace
DRY_RUN=true python -m pytest tests/ -q
DRY_RUN=true python scripts/execute_route_matrix.py --step verify-all
python scripts/rebalance_for_test.py   # fund for 31 VCHF matrix
```
