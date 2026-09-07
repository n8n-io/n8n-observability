#!/usr/bin/env python3
"""
Synthetic Prometheus exporter for the n8n Durable Scheduler dashboard.

Serves a /metrics endpoint mimicking the `n8n_scheduler_*` series with moving demo
data: multiple task types, ~5% failures, retries, rare dead-letters, a dispatch-lag
histogram, queue-depth gauges on slow waves, periodic backlog spikes, and owner
reconciliation bursts (quarantined / deleted / revived).

--instances N simulates N mains via an `instance` label (n8n-main-1..N). When N > 1
the last main is degraded (higher failures, worse lag, more dead-letters) so the
Per-Main Breakdown row diverges; leader-only work (materialization, reaper, pruning)
goes to main-1. Requires `honor_labels: true` on the Prometheus job. The cluster-wide
gauges are emitted identically for every instance.

Usage: ./scheduler-metrics-exporter.py [--port 9101] [--prefix n8n_] [--instances 1]
Standard library only.
"""

import argparse
import math
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Must match DURATION_BUCKETS_SECONDS in
# packages/cli/src/metrics/prometheus/constant.ts so histogram_quantile behaves
# exactly like it does against a real instance.
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600]

# Task types and their baseline dispatch rate (tasks/second), cluster-wide. Tune freely.
TASK_TYPES = {
    "schedule-trigger": 3.2,
    "poll-trigger": 1.1,
    "workflow-retry": 0.25,
    "cleanup": 0.15,
}

# Lag distribution over the bucket boundaries (+ overflow). Skewed low, long tail.
LAG_WEIGHTS_HEALTHY = [0.02, 0.05, 0.10, 0.28, 0.22, 0.15, 0.09, 0.05,
                       0.02, 0.008, 0.003, 0.001, 0.0005, 0.0003, 0.0001, 0.00005]
# Degraded main: shifted right, much heavier tail (slow dispatcher loop).
LAG_WEIGHTS_DEGRADED = [0.005, 0.01, 0.02, 0.06, 0.12, 0.20, 0.20, 0.16,
                        0.10, 0.06, 0.03, 0.02, 0.01, 0.004, 0.002, 0.001]


class InstanceState:
    """Per-main counters, advanced from real elapsed time."""

    def __init__(self, name: str, share: float, degraded: bool, leader: bool):
        self.name = name
        self.share = share          # fraction of cluster throughput this main handles
        self.degraded = degraded
        self.leader = leader

        self.dispatched = {t: 0.0 for t in TASK_TYPES}
        self.completed_success = {t: 0.0 for t in TASK_TYPES}
        self.completed_failure = {t: 0.0 for t in TASK_TYPES}
        self.retries = {t: 0.0 for t in TASK_TYPES}
        self.lag_buckets = {t: [0.0] * len(BUCKETS) for t in TASK_TYPES}
        self.lag_inf = {t: 0.0 for t in TASK_TYPES}
        self.lag_sum = {t: 0.0 for t in TASK_TYPES}

        # Label-free-in-code counters (still per-main once scraped).
        self.materialized = 0.0
        self.deferred = 0.0
        self.reclaimed = 0.0
        self.dead_lettered = 0.0
        self.pruned = 0.0
        self.quarantined = 0.0
        self.orphans_deleted = 0.0
        self.revived = 0.0

    def advance(self, dt: float, elapsed: float):
        weights = LAG_WEIGHTS_DEGRADED if self.degraded else LAG_WEIGHTS_HEALTHY
        for t, base_rate in TASK_TYPES.items():
            wave = 1.0 + 0.35 * math.sin(elapsed / 90.0 + hash(t) % 7)
            rate = max(0.0, base_rate * self.share * wave * random.uniform(0.85, 1.15))
            n = rate * dt
            self.dispatched[t] += n

            base_fail = 0.12 if t == "workflow-retry" else 0.05
            fail_frac = base_fail * (3.0 if self.degraded else 1.0)
            failures = n * min(fail_frac, 0.9)
            self.completed_success[t] += n - failures
            self.completed_failure[t] += failures
            self.retries[t] += failures * 0.7

            self._observe_lag(t, n, weights)

        # Dead-letters happen on any main (executor terminal-failure path); more on the degraded one.
        dl_rate = 0.01 * (6.0 if self.degraded else 1.0)
        self.dead_lettered += dl_rate * dt * random.uniform(0.0, 1.5)

        # Leader-only background jobs (materialization, reaper, retention).
        if self.leader:
            self.materialized += 6.0 * dt * random.uniform(0.8, 1.2)
            self.deferred += 0.05 * dt * random.uniform(0.0, 2.0)
            self.reclaimed += 0.08 * dt * random.uniform(0.0, 1.5)
            if math.sin(elapsed / 60.0) > 0.98:
                self.pruned += 40.0 * dt
            # Owner reconciliation sweep: short bursts on a separate phase from pruning.
            if math.sin(elapsed / 45.0 + 2.0) > 0.97:
                self.quarantined += 2.0 * dt * random.uniform(0.5, 1.5)
                self.orphans_deleted += 0.4 * dt * random.uniform(0.0, 1.5)
                self.revived += 0.6 * dt * random.uniform(0.0, 1.5)

    def _observe_lag(self, t: str, n: float, weights):
        overflow_w = 0.001 if self.degraded else 0.00005
        total_w = sum(weights) + overflow_w
        for i, w in enumerate(weights):
            share = n * w / total_w
            self.lag_buckets[t][i] += share
            self.lag_sum[t] += share * BUCKETS[i] * 0.7
        inf_share = n * overflow_w / total_w
        self.lag_inf[t] += inf_share
        self.lag_sum[t] += inf_share * 900.0


class SchedulerState:
    def __init__(self, prefix: str, instance_count: int):
        self.prefix = prefix
        self.start = time.monotonic()
        self.last = self.start

        share = 1.0 / instance_count
        self.instances = []
        for i in range(instance_count):
            degraded = instance_count > 1 and i == instance_count - 1
            self.instances.append(
                InstanceState(
                    name=f"n8n-main-{i + 1}",
                    share=share,
                    degraded=degraded,
                    leader=(i == 0),
                )
            )

    def _advance(self):
        now = time.monotonic()
        dt = now - self.last
        self.last = now
        if dt <= 0:
            return
        elapsed = now - self.start
        for inst in self.instances:
            inst.advance(dt, elapsed)

    def _gauges(self):
        # Cluster-wide snapshot: identical on every main.
        elapsed = time.monotonic() - self.start
        pending = 120 + 80 * math.sin(elapsed / 75.0) + random.uniform(-8, 8)
        running = 12 + 8 * math.sin(elapsed / 40.0 + 1.0) + random.uniform(-2, 2)
        due = max(0.0, 10 + 25 * math.sin(elapsed / 55.0 + 2.0) + random.uniform(-5, 5))
        spike = max(0.0, math.sin(elapsed / 110.0))
        oldest = (spike ** 6) * 180.0 + random.uniform(0, 1.5)
        return max(0.0, pending), max(0.0, due), max(0.0, running), max(0.0, oldest)

    def render(self) -> str:
        self._advance()
        p = self.prefix
        out = []

        def counter(name, help_text, samples):
            out.append(f"# HELP {p}{name} {help_text}")
            out.append(f"# TYPE {p}{name} counter")
            for labels, value in samples:
                lbl = "{" + labels + "}" if labels else ""
                out.append(f"{p}{name}{lbl} {value:.4f}")

        def gauge(name, help_text, per_instance):
            out.append(f"# HELP {p}{name} {help_text}")
            out.append(f"# TYPE {p}{name} gauge")
            for inst_name, value in per_instance:
                out.append(f'{p}{name}{{instance="{inst_name}"}} {value:.4f}')

        insts = self.instances

        counter(
            "scheduler_tasks_dispatched_total",
            "Total number of scheduler tasks dispatched to a handler by task type.",
            [(f'instance="{i.name}",task_type="{t}"', i.dispatched[t])
             for i in insts for t in TASK_TYPES],
        )
        counter(
            "scheduler_tasks_completed_total",
            "Total number of scheduler tasks that finished firing by task type and result.",
            [(f'instance="{i.name}",task_type="{t}",result="success"', i.completed_success[t])
             for i in insts for t in TASK_TYPES]
            + [(f'instance="{i.name}",task_type="{t}",result="failure"', i.completed_failure[t])
               for i in insts for t in TASK_TYPES],
        )
        counter(
            "scheduler_task_retries_total",
            "Total number of scheduler task retries by task type.",
            [(f'instance="{i.name}",task_type="{t}"', i.retries[t])
             for i in insts for t in TASK_TYPES],
        )

        # Histogram: cumulative buckets per instance & task type.
        out.append(f"# HELP {p}scheduler_dispatch_lag_seconds "
                   "Delay in seconds between a task becoming due and being dispatched, by task type.")
        out.append(f"# TYPE {p}scheduler_dispatch_lag_seconds histogram")
        for i in insts:
            for t in TASK_TYPES:
                cumulative = 0.0
                for idx, le in enumerate(BUCKETS):
                    cumulative += i.lag_buckets[t][idx]
                    out.append(f'{p}scheduler_dispatch_lag_seconds_bucket'
                               f'{{instance="{i.name}",task_type="{t}",le="{le}"}} {cumulative:.4f}')
                total = cumulative + i.lag_inf[t]
                out.append(f'{p}scheduler_dispatch_lag_seconds_bucket'
                           f'{{instance="{i.name}",task_type="{t}",le="+Inf"}} {total:.4f}')
                out.append(f'{p}scheduler_dispatch_lag_seconds_sum'
                           f'{{instance="{i.name}",task_type="{t}"}} {i.lag_sum[t]:.4f}')
                out.append(f'{p}scheduler_dispatch_lag_seconds_count'
                           f'{{instance="{i.name}",task_type="{t}"}} {total:.4f}')

        counter("scheduler_occurrences_materialized_total",
                "Total number of occurrences materialized from job schedules.",
                [(f'instance="{i.name}"', i.materialized) for i in insts])
        counter("scheduler_jobs_deferred_total",
                "Total number of jobs deferred for retry during materialization.",
                [(f'instance="{i.name}"', i.deferred) for i in insts])
        counter("scheduler_tasks_reclaimed_total",
                "Total number of expired scheduler tasks reclaimed by the reaper.",
                [(f'instance="{i.name}"', i.reclaimed) for i in insts])
        counter("scheduler_tasks_dead_lettered_total",
                "Total number of scheduler tasks dead-lettered after exhausting their attempts.",
                [(f'instance="{i.name}"', i.dead_lettered) for i in insts])
        counter("scheduler_tasks_pruned_total",
                "Total number of finished scheduler tasks deleted by retention.",
                [(f'instance="{i.name}"', i.pruned) for i in insts])
        counter("scheduler_jobs_quarantined_total",
                "Total number of scheduled jobs disabled by owner reconciliation because their owner was reported gone.",
                [(f'instance="{i.name}"', i.quarantined) for i in insts])
        counter("scheduler_orphaned_jobs_deleted_total",
                "Total number of quarantined scheduled jobs deleted by owner reconciliation after their owner stayed gone past the quarantine grace.",
                [(f'instance="{i.name}"', i.orphans_deleted) for i in insts])
        counter("scheduler_jobs_revived_total",
                "Total number of quarantined scheduled jobs re-enabled by owner reconciliation because their owner turned out to still exist.",
                [(f'instance="{i.name}"', i.revived) for i in insts])

        pending, due, running, oldest = self._gauges()
        names = [i.name for i in insts]
        gauge("scheduler_tasks_pending",
              "Number of pending scheduler tasks awaiting dispatch.", [(n, pending) for n in names])
        gauge("scheduler_tasks_due",
              "Number of pending scheduler tasks already due for dispatch.", [(n, due) for n in names])
        gauge("scheduler_tasks_running",
              "Number of scheduler tasks currently claimed and in flight.", [(n, running) for n in names])
        gauge("scheduler_oldest_pending_age_seconds",
              "Age in seconds of the oldest due pending scheduler task.", [(n, oldest) for n in names])

        return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9101, help="Port to listen on (default: 9101)")
    parser.add_argument("--prefix", default="n8n_", help="Metric name prefix (default: n8n_)")
    parser.add_argument("--instances", type=int, default=1,
                        help="Number of mains to simulate (default: 1). When >1, the last one is degraded.")
    args = parser.parse_args()

    if args.instances < 1:
        parser.error("--instances must be >= 1")

    state = SchedulerState(args.prefix, args.instances)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = state.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):  # silence per-request logging
            pass

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    mains = ", ".join(i.name + (" (degraded)" if i.degraded else "") for i in state.instances)
    print(f"Synthetic n8n scheduler metrics on http://localhost:{args.port}/metrics "
          f"(prefix={args.prefix!r})")
    print(f"Simulating {args.instances} main(s): {mains}")
    if args.instances > 1:
        print("Remember: Prometheus must scrape this target with `honor_labels: true`.")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
