#!/usr/bin/env bash
#
# Continuously generates webhook traffic by looping generate-webhook-traffic.sh.
# Designed to produce a realistic historic data baseline without hammering n8n.
#
# Usage:
#   ./generate-historic-webhook-traffic.sh [--batch N] [--delay S] [--fail-rate R]
#
# Options:
#   --batch N       Requests per batch (default: 15)
#   --delay S       Seconds between requests within a batch (default: 0.5)
#   --fail-rate R   Fraction of failing requests (default: 0.15)
#
# The pause between batches is randomised: 2–10 seconds in 2-second steps.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRAFFIC_SCRIPT="$SCRIPT_DIR/generate-webhook-traffic.sh"

if [[ ! -x "$TRAFFIC_SCRIPT" ]]; then
  echo "Error: $TRAFFIC_SCRIPT not found or not executable" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
BATCH=15
DELAY=0.5
FAIL_RATE=0.15

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch)     BATCH="$2";     shift 2 ;;
    --delay)     DELAY="$2";     shift 2 ;;
    --fail-rate) FAIL_RATE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
batch_num=0
trap 'echo ""; echo "Stopped after $batch_num batch(es)."; exit 0' INT TERM

# ---------------------------------------------------------------------------
# Continuous loop
# ---------------------------------------------------------------------------
echo "Historic traffic generator started (Ctrl-C to stop)"
echo "  Batch size : $BATCH requests"
echo "  In-batch delay : ${DELAY}s"
echo "  Pause between batches : 2-10s (random)"
echo "  Fail rate : $(awk "BEGIN { printf \"%.0f%%\", $FAIL_RATE * 100 }")"
echo ""

while true; do
  (( batch_num++ )) || true
  echo "=== Batch $batch_num ==="

  "$TRAFFIC_SCRIPT" \
    --requests  "$BATCH" \
    --delay     "$DELAY" \
    --fail-rate "$FAIL_RATE"

  pause=$(( (RANDOM % 5 + 1) * 2 ))
  echo "Pausing ${pause}s before next batch..."
  echo ""
  sleep "$pause"
done
