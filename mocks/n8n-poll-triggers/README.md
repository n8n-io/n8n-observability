# Mock data for the n8n Poll Triggers dashboard

Poll trigger metrics need real poll workflows erroring, overlapping, and losing
leases before the interesting panels light up. This directory ships a synthetic
Prometheus exporter that serves the `n8n_poll_trigger_*` series (plus the
poll-filtered `n8n_scheduler_*` series the dashboard reads) for demos and
screenshots without waiting for real failures.

It emits five poll node types with distinct rates and error profiles, a poll
duration histogram per node type and status (two node types are slow and overlap
their interval), errors split by kind (auth / rate_limited / thrown), cursor
commits by operation and result with rare fence rejections and failures, lease
losses, and a dispatch-lag histogram for `task_type="workflow:poll-trigger"`.
Counters advance from real elapsed time, so a few minutes at the stack's 1 s
scrape interval fills in history.

## 1. Start the exporter

Python 3 standard library only.

```bash
./poll-trigger-metrics-exporter.py         # http://localhost:9102/metrics
curl -s http://localhost:9102/metrics | grep poll_trigger_errors_total
```

### Multiple mains

`--instances N` stamps an `instance` label (`n8n-main-1` … `n8n-main-N`) on every
series to make the **Main instance** variable meaningful:

```bash
./poll-trigger-metrics-exporter.py --instances 3
```

When `N > 1` the last main is degraded (~4× error rate, slower polls, more
overlapping ticks, far more lease losses and fence rejections) so one instance
diverges when you filter by it.

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
      - targets: ["host.docker.internal:9102"]    # was 5678
    metrics_path: /metrics
```

`honor_labels: true` is only needed with `--instances > 1`; harmless otherwise.

Reload and check the target is **UP** at `http://localhost:9090/targets`:

```bash
cd local-dev && docker compose up -d
curl -X POST http://localhost:9090/-/reload    # if already running
```

Revert the target to `host.docker.internal:5678` when done.

The exporter emits its own `n8n_scheduler_dispatch_lag_seconds` and
`n8n_scheduler_tasks_lease_lost_total` for the poll task type — don't scrape it
alongside the [scheduler mock](../n8n-scheduler/) on the same series names; run
one at a time.

## 3. Screenshot

Open Grafana (http://localhost:3000, admin/admin) → **n8n Poll Triggers**. Let
it run 5–10 minutes, set the range to **Last 1 hour**. Expand the collapsed
**Per-Node-Type Breakdown** row — the Google Drive and Google Sheets triggers
stand out with slow p99 and overlapping ticks.

## Alternative: real data

Activate poll trigger workflows with short intervals (e.g. an RSS Feed Trigger
polling every minute, plus one with a broken credential for auth errors) with
`N8N_METRICS=true` and `N8N_METRICS_INCLUDE_POLL_TRIGGER_METRICS=true` (add
`N8N_METRICS_INCLUDE_SCHEDULER_METRICS=true` for the lease-lost and dispatch-lag
panels). Authentic, but slow to build history and won't easily produce
overlapping ticks, lease losses, or fence rejections.
