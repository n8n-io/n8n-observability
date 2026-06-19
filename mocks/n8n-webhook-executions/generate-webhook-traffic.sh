#!/usr/bin/env bash
#
# Generates traffic against a list of webhook URLs to produce demo data
# for the n8n-webhook-executions Grafana dashboard.
#
# Usage:
#   ./generate-webhook-traffic.sh [--requests N] [--delay S] [--fail-rate R]
#
# Options:
#   --requests N    Total number of requests to send (default: 200)
#   --delay S       Seconds to sleep between requests (default: 0.2)
#   --fail-rate R   Fraction of requests that use a "bad" method (default: 0.15)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — add or remove webhook URLs here
# ---------------------------------------------------------------------------
WEBHOOK_URLS=(
  "http://localhost:5678/webhook/098ad453-5b09-4c2c-bc3c-6fae90939ae9"
  "http://localhost:5678/webhook/294febd8-8e32-42f3-8fb3-b6a9f88cd915"
  "http://localhost:5678/webhook/a5af11f1-6216-4afb-bd99-7641cd431108"
  "http://localhost:5678/webhook/a50856e9-91d4-42f0-9603-8b0de6ff29cd"
)

# Query parameter appended to failing requests
FAIL_PARAM="fail=true"

# ---------------------------------------------------------------------------
# Defaults (overridable via flags)
# ---------------------------------------------------------------------------
TOTAL_REQUESTS=50
DELAY=0.2
FAIL_RATE=0.15   # 0.0–1.0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --requests) TOTAL_REQUESTS="$2"; shift 2 ;;
    --delay)    DELAY="$2";          shift 2 ;;
    --fail-rate) FAIL_RATE="$2";     shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
random_element() {
  local arr=("$@")
  echo "${arr[RANDOM % ${#arr[@]}]}"
}

# Returns 1 (true in bash exit-code terms) with probability $FAIL_RATE
should_fail() {
  # bc-free comparison: scale FAIL_RATE to an integer threshold out of 100
  local threshold
  threshold=$(awk "BEGIN { printf \"%d\", $FAIL_RATE * 100 }")
  local roll=$(( RANDOM % 100 ))
  (( roll < threshold ))
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
success_count=0
fail_count=0

echo "Sending $TOTAL_REQUESTS requests to ${#WEBHOOK_URLS[@]} webhook(s)..."
echo "Fail rate: $(awk "BEGIN { printf \"%.0f%%\", $FAIL_RATE * 100 }")"
echo "Delay between requests: ${DELAY}s"
echo ""

for (( i = 1; i <= TOTAL_REQUESTS; i++ )); do
  url=$(random_element "${WEBHOOK_URLS[@]}")

  if should_fail; then
    request_url="${url}?${FAIL_PARAM}"
    label="FAIL"
    (( fail_count++ )) || true
  else
    request_url="$url"
    label="OK  "
    (( success_count++ )) || true
  fi

  http_status=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$request_url" || true)

  printf "[%3d/%d] %s  %s  ->  HTTP %s\n" \
    "$i" "$TOTAL_REQUESTS" "$label" "$request_url" "$http_status"

  sleep "$DELAY"
done

echo ""
echo "Done. Success: $success_count  |  Intentional failures: $fail_count"
