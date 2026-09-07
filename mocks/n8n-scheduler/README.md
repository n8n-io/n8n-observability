# Mock data for the n8n Durable Scheduler dashboard

The scheduler can't be driven with `curl` like webhooks can — its metrics come
from its own dispatch/retry/reaper loops. This directory ships a synthetic
Prometheus exporter that serves `n8n_scheduler_*` series for demos and screenshots
without waiting for real backlog and failures.

It emits multiple task types, a ~5% failure rate, occasional retries, rare
dead-letters, a dispatch-lag histogram (p50 ≈ 50 ms, p90 ≈ 0.5 s, p99 ≈ 2.5 s),
queue-depth gauges on slow waves, periodic "oldest pending age" spikes, and owner
reconciliation bursts (quarantined / deleted / revived jobs on the leader main).
Counters advance from real elapsed time, so a few minutes at the stack's 1 s
scrape interval fills in history.

## 1. Start the exporter

Python 3 standard library only.

```bash
./scheduler-metrics-exporter.py            # http://localhost:9101/metrics
curl -s http://localhost:9101/metrics | grep scheduler_tasks_pending
```

### Multiple mains

`--instances N` stamps an `instance` label (`n8n-main-1` … `n8n-main-N`) on every
series to populate the **Per-Main Breakdown** row:

```bash
./scheduler-metrics-exporter.py --instances 3
```

When `N > 1` the last main is degraded (~3× failure rate, worse lag p99, more
dead-letters) so one instance diverges. Leader-only work (materialization, reaper,
retention) is attributed to `n8n-main-1`; the cluster-wide gauges stay identical
on every main.

This requires `honor_labels: true` on the Prometheus job (see below), otherwise
Prometheus overwrites `instance` with the target address and the mains collapse
into one.

## 2. Point Prometheus at it

Point the `local-dev/` stack at the exporter instead of n8n, so nothing
double-counts. Edit
[`local-dev/prometheus/prometheus.yml`](../../local-dev/prometheus/prometheus.yml):

```yaml
scrape_configs:
  - job_name: "n8n"
    honor_labels: true                            # keep the exporter's instance labels
    static_configs:
      - targets: ["host.docker.internal:9101"]    # was 5678
    metrics_path: /metrics
```

`honor_labels: true` is only needed with `--instances > 1`; harmless otherwise.

Reload and check the target is **UP** at `http://localhost:9090/targets`:

```bash
cd local-dev && docker compose up -d
curl -X POST http://localhost:9090/-/reload    # if already running
```

Revert the target to `host.docker.internal:5678` when done.

### Alternative: one target per main

One process per port, one Prometheus target each — no `honor_labels`, but every
main behaves identically (no degraded one):

```bash
./scheduler-metrics-exporter.py --port 9101 &
./scheduler-metrics-exporter.py --port 9102 &
```

```yaml
    static_configs:
      - targets:
          - "host.docker.internal:9101"
          - "host.docker.internal:9102"
```

## 3. Screenshot

Open Grafana (http://localhost:3000, admin/admin) → **n8n Durable Scheduler**. Let
it run 5–10 minutes, set the range to **Last 1 hour**. With `--instances 3`,
expand the collapsed **Per-Main Breakdown** row — `n8n-main-3` diverges there.

## Alternative: real data

Create Schedule Trigger workflows with short intervals (and a couple that error,
for failures/retries) with `N8N_METRICS=true` and
`N8N_METRICS_INCLUDE_SCHEDULER_METRICS=true`. Authentic, but slow to build history
and won't easily produce dead-letters or a lag-percentile spread.
