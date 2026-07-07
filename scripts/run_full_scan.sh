#!/usr/bin/env bash
# Resumable full Kalshi fee scan for VPS bootstrap.
# Re-run safely after API timeouts; exits 0 only when scan quality checks pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CHECKPOINT="${CHECKPOINT:-data/checkpoints/scan.json}"
OUTPUT_DIR="${OUTPUT_DIR:-.}"
LOG="${LOG:-logs/full-scan.log}"

mkdir -p "$(dirname "$CHECKPOINT")" "$(dirname "$LOG")"
pip install -q -r requirements.txt

echo "=== Kalshi full scan started $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOG"

while true; do
  if PYTHONUNBUFFERED=1 python3 scripts/kalshi_fee_calculator.py \
      --output-dir "$OUTPUT_DIR" \
      --checkpoint "$CHECKPOINT" \
      --resume \
      --fail-on-incomplete 2>&1 | tee -a "$LOG"; then
    echo "=== Scan complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOG"
    exit 0
  fi
  code=$?
  if [[ "$code" -eq 2 ]]; then
    echo "=== Scan incomplete (quality checks failed); resuming in 60s ===" | tee -a "$LOG"
    sleep 60
    continue
  fi
  echo "=== Scan errored (exit $code); resuming in 120s ===" | tee -a "$LOG"
  sleep 120
done