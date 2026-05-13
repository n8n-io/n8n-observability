#!/bin/sh
set -e

PROV_SRC=/etc/grafana/provisioning-src
PROV_DIR=/var/lib/grafana/provisioning

rm -rf "$PROV_DIR"
mkdir -p "$PROV_DIR"
cp -r "$PROV_SRC/." "$PROV_DIR/"

if [ -z "${GRAFANA_SLACK_TOKEN}" ]; then
  echo "GRAFANA_SLACK_TOKEN is not set — skipping Slack alerting provisioning"
  rm -rf "$PROV_DIR/alerting"
fi

export GF_PATHS_PROVISIONING="$PROV_DIR"
exec /run.sh "$@"
