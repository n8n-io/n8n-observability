# n8n Metrics — Local Grafana + Prometheus

Spins up a local Prometheus + Grafana stack to visualize metrics from a locally running n8n instance.

## Prerequisites

**Enable metrics in n8n** by setting the following environment variable before starting n8n:

```
N8N_METRICS=true
```

Verify it's working by visiting `http://localhost:5678/metrics` — you should see Prometheus-formatted output.

## Start

```bash
docker compose up -d
```

## Access

| Service    | URL                          | Credentials     |
|------------|------------------------------|-----------------|
| Grafana    | http://localhost:3000        | admin / admin   |
| Prometheus | http://localhost:9090        | —               |

## Verify scraping

Open `http://localhost:9090/targets` — the `n8n` target should show state **UP**.

## Adding dashboards

Drop any Grafana dashboard `.json` file into `dashboards/`. Grafana polls that directory every 30 seconds and picks up new or changed files automatically — no restart needed.

## Configuring n8n environments

The `n8n Webhook Executions` dashboard has an **Environment** picker at the top that controls where the workflow-name links open (e.g. clicking a workflow opens `<base-url>/workflow/<id>` in n8n).

To add your own environments, edit the `n8n_base_url` variable in `dashboards/n8n-webhook-executions.json`:

```json
{
  "name": "n8n_base_url",
  "label": "Environment",
  "type": "custom",
  "query": "Local : http://localhost:5678, Staging : https://n8n.staging.example.com, Production : https://n8n.example.com",
  "options": [
    { "text": "Local",      "value": "http://localhost:5678",           "selected": true  },
    { "text": "Staging",    "value": "https://n8n.staging.example.com", "selected": false },
    { "text": "Production", "value": "https://n8n.example.com",         "selected": false }
  ],
  "current": { "text": "Local", "value": "http://localhost:5678", "selected": true }
}
```

The `query` field uses Grafana's `Label : value, Label : value` syntax; keep the `options` array in sync. Grafana picks up the change within 30 seconds — no restart needed.

## Slack alerts (optional)

A bundled alert fires a Slack notification when a workflow produces more than 5 failed webhook executions in 30 seconds (see `grafana/provisioning/alerting/`).

To enable it, set both env vars before starting the stack:

```bash
export GRAFANA_SLACK_TOKEN=xoxb-...   # Slack bot token with chat:write scope
export GRAFANA_SLACK_CHANNEL=C0123ABC  # channel ID to post to
docker compose up -d
```

If either is unset, the entrypoint script skips Slack alert provisioning entirely so Grafana boots without it.

To fire the configured slack alert for testing purposes, use our mock script like this: `./mocks/generate-webhook-traffic.sh --requests 20 --delay 0 --fail-rate 1.0`

## Stop

```bash
docker compose down
```

Data is persisted in named Docker volumes (`n8n_prometheus_data`, `n8n_grafana_data`). To wipe it:

```bash
docker compose down -v
```

## Configuration

| File | Purpose |
|------|---------|
| `prometheus/prometheus.yml` | Scrape config — change the target host/port here |
| `grafana/provisioning/datasources/prometheus.yml` | Grafana datasource (auto-provisioned) |
| `dashboards/` | Dashboard JSON files — auto-loaded with live reload |
