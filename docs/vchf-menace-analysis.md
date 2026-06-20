# VCHF Menace — deep analysis

Adapted from **GBP Menace** (`f99144c`) for **VCHF** platform-centric arbitrage.  
Repo: https://github.com/Giansensey007/vchf_menace

---

## 1. Route map vs GBP (1:1)

| GBP Menace direction | VCHF Menace direction | Buy leg | Sell leg | Rebalance |
|----------------------|----------------------|---------|----------|-----------|
| `celo_to_solana` | `celo_to_solana` | Celo USDT → VCHF | Sol VCHF → USDC | VNX bridge |
| `solana_to_celo` | `solana_to_celo` | Sol USDC → VCHF | Celo VCHF → USDT | VNX bridge |
| `celo_to_vnx` | `celo_to_vnx` | Celo USDT → VCHF | VNX sell VCHF | deposit |
| `vnx_to_celo` | `vnx_to_celo` | VNX buy VCHF | Celo VCHF → USDT | withdraw |
| `solana_to_vnx` | `solana_to_vnx` | Sol USDC → VCHF | VNX sell VCHF | deposit |
| `vnx_to_solana` | `vnx_to_solana` | VNX buy VCHF | Sol VCHF → USDC | withdraw |
| CCTP return after `vnx_to_solana` | same | Sol USDC → ETH → VNX USDC → buy VCHF | — | `use_cctp_usdc_return()` |
| Wormhole hub (USDT) | same | Celo ↔ ETH ↔ Sol stables | — | `execute_route_matrix` hub steps |

All six arb directions, CCTP USDC return, and Wormhole/CCTP hub legs mirror GBP with VCHF token/pool addresses and `platform_vchf_only` treasury naming.

---

## 2. VCHF-specific VNX minimums (API)

Fetched from VNX `get_trading_pairs()` (live API, 2026-06-20):

| Pair | Status | `min_order_size` | Notes |
|------|--------|------------------|-------|
| **VCHF/USDC** | online | **30** | Used by bot (`VCHF_MIN_ORDER`, route test @ 31) |
| VCHF/ETH | online | 30 | — |
| VCHF/BTC | online | 50 | — |
| VCHF/CHF | online | 0.1 | Fiat rail |
| VGBP/USDC (GBP ref) | online | 40 | GBP uses 40; VCHF is 30 |

On-chain deposit minimums (unchanged from GBP pattern):

| Chain | Asset | Min cumulative |
|-------|-------|----------------|
| CELO | VCHF | 5 |
| SOL | VCHF | 5 |
| ETH | USDC | 20 |

Code constants: `src/vnx/trading.py` → `VCHF_MIN_ORDER = 30.0`; `src/quotes/vnx.py` → `VNX_MIN_ORDER["VCHF"] = 30.0`.

---

## 3. Token addresses

| Chain | VCHF mint / contract |
|-------|----------------------|
| Celo | `0xc5ebea9984c485ec5d58ca5a2d376620d93af871` |
| Solana | `AhhdRu5YZdjVkKR3wbnUDaymVQL2ucjMQ63sZ3LFHsch` |
| VNX Platform | `VCHF` (symbol) |

Celo DEX pool (Uniswap V3): `vchf_usdt` @ `0x899f68521196b4db5e3525e8ce1695efa9b05533` (fee 100, `token0_is_vchf: true`).

Source: VNX Telegram Chat `config/tokens.yaml` + `config/chains.yaml` (same as VNX canonical deployments).

---

## 4. Production route — Celo → Sol closed loop

**User target:** Platform VCHF → withdraw CELO → sell VCHF→USDT → bridge to Solana → buy VCHF with USDC → deposit VCHF to platform.

### Step-by-step

| # | Action | Implementation |
|---|--------|----------------|
| 1 | Withdraw VCHF from VNX to Celo hot wallet | `VnxBridge.bridge_vchf(..., withdraw_only=True)` in `vnx_to_celo` |
| 2 | Swap VCHF → USDT on Celo | Celo Uniswap V3 via `CeloExecutor` |
| 3 | Bridge USDT Celo → Solana | Wormhole Portal (`wormhole_celo_to_sol_direct` in hub scripts) |
| 4 | Acquire VCHF on Solana | Jupiter: USDC → VCHF (may require USDT→USDC hop if only USDT landed) |
| 5 | Deposit VCHF to VNX | `solana_to_vnx` — SPL transfer + VNX deposit poll |

### Mapping to route IDs

- Primary leg: **`vnx_to_celo`** (ends with USDT on Celo)
- Hub bridge: **`execute_route_matrix`** wormhole preflight / `celo_usdt_to_sol_usdc`
- Return leg: **`solana_to_vnx`** (or **`celo_to_solana` inverse** depending on capital location)

Treasury helpers:

- `platform_vchf_only=true` — never hold VCHF on-chain except transient swap/bridge amounts
- `consolidate_vchf_to_platform()` — sweeps dust after each cycle
- `close_loop_always_return=true` — runs inverse leg for capital homing

Alternative shorter path when Sol spread is better: **`vnx_to_solana`** + CCTP USDC return (`use_cctp_usdc_return`).

---

## 5. Test results

### Pytest

```
DRY_RUN=true python -m pytest tests/ -q
140 passed
```

### verify-all (with production `.env`, DRY_RUN=true)

| Branch | Result |
|--------|--------|
| cctp_claim | PASS |
| wormhole_claim | PASS |
| wormhole_preflight | PASS |
| route_simulations (6 dirs @ 31 VCHF) | PASS |
| celo_swaps | PASS |
| sol_swaps | SKIP (low Sol USDC) |
| platform_probe | SKIP (low platform USDC) |
| eth_to_vnx / vnx_to_eth | SKIP (below 20 USDC VNX min) |
| cctp_sol_eth / cctp_eth_sol | SKIP (insufficient hub USDC) |

Critical preflight branches (`cctp_claim`, `wormhole_claim`, `wormhole_preflight`, `route_simulations`) **all PASS**.

---

## 6. Funding thresholds before live

From `config/production.yaml`:

### Production deploy

| Location | Minimum |
|----------|---------|
| Platform VCHF | 200 |
| Platform USDC | 250 |
| Celo USDT | 250 |
| Sol USDC | 250 |
| ETH USDC | 50 |
| ETH USDT | 50 |
| ETH native | 0.015 |
| CELO native | 0.5 |
| SOL native | 0.05 |

### Route test (31 VCHF matrix)

| Location | Minimum |
|----------|---------|
| Platform VCHF | 32 |
| Platform USDC | 45 |
| Celo USDT | 45 |
| Sol USDC | 45 |

Current shared hot wallet (GBP/VCHF keys): **under-funded** on all production targets; fund before `DRY_RUN=false`.

---

## 7. Gaps / notes

1. **Same VNX account as GBP** — VCHF and VGBP share one platform balance; run one Menace bot at a time or use separate VNX API keys per bot.
2. **On-chain probes skipped** in verify-all until stables funded (expected).
3. **Celo sell probe** logs `AS` revert on gas estimate in dry-run — broadcast path uses same router as GBP; live swap requires ≥5 VCHF on Celo for deposit credit.
4. **PDF docs** (`docs/gbp-menace-*.pdf`) copied from template — regenerate with `scripts/generate_routes_pdf.py` for VCHF branding if needed.

---

## 8. Workspace wiring

| Item | Path |
|------|------|
| Local bot | `environment/VCHF_Menace/` |
| Nested git | `environment/VCHF_Menace/.git` |
| GitHub | https://github.com/Giansensey007/vchf_menace |
| Registry | `environment/REGISTRY.md` |
| Push routing | `.cursor/rules/git-push-routing.mdc` |
