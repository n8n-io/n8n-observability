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

Drop any Grafana dashboard `.json` file into `grafana/provisioning/dashboards/`. Grafana picks it up automatically within 30 seconds (no restart needed).

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
| `grafana/provisioning/dashboards/` | Place dashboard JSON files here |
