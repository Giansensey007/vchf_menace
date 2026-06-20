#!/usr/bin/env bash
# 10-iteration production sanity: pytest + audit; even iters also verify-all
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PWD}/.venv/bin/python"
export DRY_RUN=true
LOG="${PWD}/docs/sanity_10iter_round5.tsv"
echo -e "iter\tpytest\taudit\tverify_all" > "$LOG"
for i in $(seq 1 10); do
  PT=$($PY -m pytest tests/ -q 2>&1 | tail -1)
  PT_OK=$([[ "$PT" == *"passed"* ]] && echo PASS || echo FAIL)
  $PY scripts/execute_route_matrix.py --step audit >/dev/null 2>&1 && AU=PASS || AU=FAIL
  VA="—"
  if (( i % 2 == 0 )); then
    $PY scripts/execute_route_matrix.py --step verify-all >/dev/null 2>&1 && VA=PASS || VA=FAIL
  fi
  echo -e "${i}\t${PT_OK}\t${AU}\t${VA}" | tee -a "$LOG"
done
echo "DONE $LOG"
