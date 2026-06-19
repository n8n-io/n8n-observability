#!/bin/sh
set -e

PROV_SRC=/etc/grafana/provisioning-src
PROV_DIR=/var/lib/grafana/provisioning

rm -rf "$PROV_DIR"
mkdir -p "$PROV_DIR"
cp -r "$PROV_SRC/." "$PROV_DIR/"

if [ -z "${GRAFANA_SLACK_TOKEN}" ] || [ -z "${GRAFANA_SLACK_CHANNEL}" ]; then
  echo "GRAFANA_SLACK_TOKEN or GRAFANA_SLACK_CHANNEL is not set — skipping Slack alerting provisioning"
  rm -f "$PROV_DIR/alerting"/*.yaml "$PROV_DIR/alerting"/*.yml
fi

export GF_PATHS_PROVISIONING="$PROV_DIR"
exec /run.sh "$@"
