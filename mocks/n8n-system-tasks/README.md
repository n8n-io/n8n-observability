# Mock data for the n8n System Tasks dashboard

You can't drive system tasks with `curl` the way you can drive webhooks: their
metrics come from timers inside the leader main instance. A real instance also
makes a poor demo, because cadences run in minutes and hours, nothing fails,
nothing is skipped and no task is durable yet, so most of the dashboard stays
flat. This directory ships a synthetic Prometheus exporter that serves
`n8n_system_task_*` series straight away.

It simulates the 15 system tasks n8n registers today. Runs fire occurrence by
occurrence, the way real timers do, rather than as a smooth rate. The demo
covers:

| Task | What it demonstrates |
|------|----------------------|
| `mcp-registry-refresh` | A task that stopped being scheduled. It lights up **Tasks Not Scheduled**, climbs on **Overdue Factor**, and falls past zero on **Time to Next Run**, because it keeps the occurrence it was armed for when it stopped |
| `workflow-history-compaction-optimize` | A task with no recorded success yet, as just after a leader takeover. This is the purple line on **Overdue Factor** |
| `insights-pruning` | Fails a quarter of its runs and schedules retries |
| `workflow-history-compaction-trim` | Runs long enough to skip occurrences as `overlap`, plus the odd `coalesced` fire |
| `publication-outbox-cleanup` | Caught mid-handover to the durable scheduler, so occurrences skip as `provisioned_elsewhere` |
| `activity-pruning`, `execution-pruning-soft-delete` | Run in `durable` mode, and also emit the durable scheduler's own `n8n_scheduler_*` series as `task_type="system:<name>"` |

Cadences are compressed to seconds, so a screenshot takes minutes instead of a
day. `system_task_interval_seconds` reports the compressed cadence, so the overdue
factor (age of last success divided by cadence) still reads correctly. The
histogram buckets are the real `DURATION_BUCKETS_SECONDS` and
`LAG_BUCKETS_SECONDS`, so the quantile panels behave just as they do against a
real instance.

## 1. Start the exporter

Python 3 standard library only.

```bash
./system-task-metrics-exporter.py            # http://localhost:9102/metrics
curl -s http://localhost:9102/metrics | grep system_task_scheduled
```

### Multiple mains

`--instances N` stamps an `instance` label (`n8n-main-1` … `n8n-main-N`) on every
series:

```bash
./system-task-metrics-exporter.py --instances 3
```

As in a real cluster, in-memory series exist only on the leader (`n8n-main-1`),
which is what the **Leaders per Task** panel reads, while durable series exist on
every main.

This needs `honor_labels: true` on the Prometheus job (see below). Without it,
Prometheus overwrites `instance` with the target address and all the mains
collapse into one.

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

`honor_labels: true` is only needed with `--instances > 1`, and is harmless
otherwise.

Reload and check the target is **UP** at `http://localhost:9090/targets`:

```bash
cd local-dev && docker compose up -d
curl -X POST http://localhost:9090/-/reload    # if already running
```

Set the target back to `host.docker.internal:5678` when you are done.

### Running it alongside the scheduler mock

The two exporters use different ports (9101 and 9102) and different metric names,
so Prometheus can scrape both from the same job and both dashboards fill in at
once.

## 3. Screenshot

Open Grafana (http://localhost:3000, admin/admin) → **n8n System Tasks**. Let it
run 5–10 minutes so the counters build up history, then set the range to **Last 7
minutes**: long enough for every cadence to repeat, short enough to still see
individual runs. Expand the collapsed **Leadership & Durable Scheduler** row for
the leader count and the two durable scheduler panels.

## Alternative: real data

Run a main with `N8N_METRICS=true` and
`N8N_METRICS_INCLUDE_SYSTEM_TASK_METRICS=true`, and shorten whichever cadences are
configurable. `EXECUTIONS_DATA_PRUNE_SOFT_DELETE_INTERVAL` is one. This is
authentic, but slow to build history, and on its own it produces no failures, no
skipped occurrences and no durable runs.
