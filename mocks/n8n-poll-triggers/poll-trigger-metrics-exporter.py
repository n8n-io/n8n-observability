#!/usr/bin/env python3
"""
Synthetic Prometheus exporter for the n8n Poll Triggers dashboard.

Serves a /metrics endpoint mimicking the `n8n_poll_trigger_*` series (plus the
poll-filtered `n8n_scheduler_*` series the dashboard reads) with moving demo data:
several poll node types, a duration histogram per node type and status, errors
split by kind (auth / rate_limited / thrown), occasional same-process overlapping
ticks, cursor commits by operation and result with rare fence rejections and
failures, lease losses, and a dispatch-lag histogram for the poll task type.

--instances N simulates N mains via an `instance` label (n8n-main-1..N). When
N > 1 the last main is degraded (more errors, slower polls, more overlap, lease
losses and fence rejections) so per-instance filtering diverges. Requires
`honor_labels: true` on the Prometheus job.

Usage: ./poll-trigger-metrics-exporter.py [--port 9102] [--prefix n8n_] [--instances 1]
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

# Poll node types: baseline tick rate (ticks/second, cluster-wide), error fraction,
# and a duration profile ("fast" or "slow"). The slow one overlaps its interval.
NODE_TYPES = {
    "n8n-nodes-base.gmailTrigger":        {"rate": 0.9,  "errors": 0.02, "profile": "fast"},
    "n8n-nodes-base.rssFeedReadTrigger":  {"rate": 1.4,  "errors": 0.01, "profile": "fast"},
    "n8n-nodes-base.airtableTrigger":     {"rate": 0.5,  "errors": 0.08, "profile": "fast"},
    "n8n-nodes-base.googleDriveTrigger":  {"rate": 0.35, "errors": 0.03, "profile": "slow"},
    "n8n-nodes-base.googleSheetsTrigger": {"rate": 0.25, "errors": 0.05, "profile": "slow"},
}

# How errors split into kinds (auth, rate_limited, thrown).
ERROR_KIND_SPLIT = {"auth": 0.25, "rate_limited": 0.35, "thrown": 0.40}

# Duration distribution over the bucket boundaries (+ overflow). Fast: sub-second.
DURATION_WEIGHTS_FAST = [0.01, 0.03, 0.10, 0.20, 0.28, 0.20, 0.10, 0.05,
                         0.02, 0.007, 0.002, 0.0005, 0.0002, 0.0001, 0.00005, 0.00002]
# Slow: seconds, tail into tens of seconds — the overlap-prone profile.
DURATION_WEIGHTS_SLOW = [0.001, 0.002, 0.01, 0.03, 0.06, 0.12, 0.20, 0.24,
                         0.18, 0.09, 0.04, 0.015, 0.006, 0.002, 0.0005, 0.0002]
# Degraded main: everything shifted right, heavier tail.
DURATION_WEIGHTS_DEGRADED = [0.0005, 0.001, 0.005, 0.015, 0.04, 0.09, 0.16, 0.22,
                             0.21, 0.13, 0.07, 0.035, 0.015, 0.006, 0.002, 0.001]

CURSOR_OPERATIONS = ("with_execution", "cursor_only")
# Cursor commits are quick DB transactions; with_execution is a bit slower.
COMMIT_WEIGHTS = {
    "with_execution": [0.02, 0.08, 0.22, 0.30, 0.22, 0.10, 0.04, 0.015,
                       0.004, 0.001, 0.0003, 0.0001, 0.00005, 0.00002, 0.00001, 0.000005],
    "cursor_only":    [0.10, 0.25, 0.30, 0.20, 0.10, 0.035, 0.01, 0.004,
                       0.001, 0.0003, 0.0001, 0.00003, 0.00001, 0.000005, 0.000002, 0.000001],
}

POLL_TASK_TYPE = "workflow:poll-trigger"

# Poll dispatch lag (durable path): skewed low, long tail.
LAG_WEIGHTS = [0.02, 0.05, 0.10, 0.28, 0.22, 0.15, 0.09, 0.05,
               0.02, 0.008, 0.003, 0.001, 0.0005, 0.0003, 0.0001, 0.00005]


class Histogram:
    """Cumulative-bucket histogram fed by weighted shares of n observations."""

    def __init__(self):
        self.buckets = [0.0] * len(BUCKETS)
        self.inf = 0.0
        self.sum = 0.0

    def observe_n(self, n: float, weights, overflow_w: float, overflow_value: float):
        total_w = sum(weights) + overflow_w
        for i, w in enumerate(weights):
            share = n * w / total_w
            self.buckets[i] += share
            self.sum += share * BUCKETS[i] * 0.7
        inf_share = n * overflow_w / total_w
        self.inf += inf_share
        self.sum += inf_share * overflow_value

    def render(self, out, name, labels):
        cumulative = 0.0
        for idx, le in enumerate(BUCKETS):
            cumulative += self.buckets[idx]
            out.append(f'{name}_bucket{{{labels},le="{le}"}} {cumulative:.4f}')
        total = cumulative + self.inf
        out.append(f'{name}_bucket{{{labels},le="+Inf"}} {total:.4f}')
        out.append(f'{name}_sum{{{labels}}} {self.sum:.4f}')
        out.append(f'{name}_count{{{labels}}} {total:.4f}')
        return total


class InstanceState:
    """Per-main counters, advanced from real elapsed time."""

    def __init__(self, name: str, share: float, degraded: bool):
        self.name = name
        self.share = share          # fraction of cluster poll throughput this main handles
        self.degraded = degraded

        self.duration = {t: {"success": Histogram(), "error": Histogram()} for t in NODE_TYPES}
        self.errors = {t: {k: 0.0 for k in ERROR_KIND_SPLIT} for t in NODE_TYPES}
        self.overlaps = {t: 0.0 for t in NODE_TYPES}

        self.cursor_commits = {(op, res): 0.0
                               for op in CURSOR_OPERATIONS
                               for res in ("success", "fence_rejected", "failure")}
        self.cursor_duration = {(op, res): Histogram()
                                for op in CURSOR_OPERATIONS
                                for res in ("success", "fence_rejected", "failure")}

        self.lease_lost = 0.0
        self.dispatch_lag = Histogram()

    def advance(self, dt: float, elapsed: float):
        err_mult = 4.0 if self.degraded else 1.0
        for t, cfg in NODE_TYPES.items():
            wave = 1.0 + 0.3 * math.sin(elapsed / 80.0 + hash(t) % 7)
            n = max(0.0, cfg["rate"] * self.share * wave * random.uniform(0.85, 1.15)) * dt

            err_frac = min(cfg["errors"] * err_mult, 0.9)
            errors = n * err_frac
            successes = n - errors

            if self.degraded:
                weights = DURATION_WEIGHTS_DEGRADED
            else:
                weights = DURATION_WEIGHTS_SLOW if cfg["profile"] == "slow" else DURATION_WEIGHTS_FAST
            overflow_w = 0.001 if self.degraded else 0.0001
            self.duration[t]["success"].observe_n(successes, weights, overflow_w, 900.0)
            # Errors surface faster on average (fail-fast HTTP errors).
            self.duration[t]["error"].observe_n(errors, DURATION_WEIGHTS_FAST, overflow_w, 900.0)

            for kind, split in ERROR_KIND_SPLIT.items():
                self.errors[t][kind] += errors * split

            # Slow profiles overlap their interval; the degraded main overlaps everywhere.
            overlap_frac = 0.06 if cfg["profile"] == "slow" else 0.002
            if self.degraded:
                overlap_frac *= 4.0
            self.overlaps[t] += n * overlap_frac * random.uniform(0.5, 1.5)

            # Every successful tick commits its cursor; ~35% of ticks found items.
            with_exec = successes * 0.35
            cursor_only = successes * 0.65
            fence_frac = (0.02 if self.degraded else 0.002) * random.uniform(0.5, 1.5)
            fail_frac = (0.01 if self.degraded else 0.001) * random.uniform(0.5, 1.5)
            for op, count in (("with_execution", with_exec), ("cursor_only", cursor_only)):
                fenced = count * fence_frac
                failed = count * fail_frac
                ok = count - fenced - failed
                for res, c in (("success", ok), ("fence_rejected", fenced), ("failure", failed)):
                    self.cursor_commits[(op, res)] += c
                    self.cursor_duration[(op, res)].observe_n(
                        c, COMMIT_WEIGHTS[op], 0.00001, 300.0)

        # Lease losses: rare cross-main overlap; the degraded main loses far more.
        ll_rate = 0.004 * (8.0 if self.degraded else 1.0)
        self.lease_lost += ll_rate * dt * random.uniform(0.0, 1.5)

        # Durable dispatch lag for poll tasks handled by this main.
        total_rate = sum(cfg["rate"] for cfg in NODE_TYPES.values()) * self.share
        self.dispatch_lag.observe_n(total_rate * dt, LAG_WEIGHTS,
                                    0.001 if self.degraded else 0.00005, 900.0)


class PollTriggerState:
    def __init__(self, prefix: str, instance_count: int):
        self.prefix = prefix
        self.start = time.monotonic()
        self.last = self.start

        share = 1.0 / instance_count
        self.instances = [
            InstanceState(
                name=f"n8n-main-{i + 1}",
                share=share,
                degraded=(instance_count > 1 and i == instance_count - 1),
            )
            for i in range(instance_count)
        ]

    def _advance(self):
        now = time.monotonic()
        dt = now - self.last
        self.last = now
        if dt <= 0:
            return
        elapsed = now - self.start
        for inst in self.instances:
            inst.advance(dt, elapsed)

    def render(self) -> str:
        self._advance()
        p = self.prefix
        out = []

        def counter(name, help_text, samples):
            out.append(f"# HELP {p}{name} {help_text}")
            out.append(f"# TYPE {p}{name} counter")
            for labels, value in samples:
                out.append(f"{p}{name}{{{labels}}} {value:.4f}")

        insts = self.instances

        out.append(f"# HELP {p}poll_trigger_duration_seconds "
                   "Duration in seconds of a poll trigger's poll() call, by node type and status.")
        out.append(f"# TYPE {p}poll_trigger_duration_seconds histogram")
        for i in insts:
            for t in NODE_TYPES:
                for status in ("success", "error"):
                    i.duration[t][status].render(
                        out, f"{p}poll_trigger_duration_seconds",
                        f'instance="{i.name}",node_type="{t}",status="{status}"')

        counter(
            "poll_trigger_errors_total",
            "Total number of poll trigger ticks that threw, by node type and error kind "
            "(auth, rate_limited, thrown).",
            [(f'instance="{i.name}",node_type="{t}",kind="{k}"', i.errors[t][k])
             for i in insts for t in NODE_TYPES for k in ERROR_KIND_SPLIT],
        )
        counter(
            "poll_trigger_overlapping_ticks_total",
            "Total number of poll ticks that started while another tick for the same node "
            "was still in flight in this process.",
            [(f'instance="{i.name}",node_type="{t}"', i.overlaps[t])
             for i in insts for t in NODE_TYPES],
        )
        counter(
            "poll_trigger_cursor_commits_total",
            "Total number of poll cursor commits by operation and result "
            "(success, fence_rejected, failure).",
            [(f'instance="{i.name}",operation="{op}",result="{res}"', v)
             for i in insts for (op, res), v in i.cursor_commits.items()],
        )

        out.append(f"# HELP {p}poll_trigger_cursor_commit_duration_seconds "
                   "Duration in seconds of poll cursor commits, by operation and result.")
        out.append(f"# TYPE {p}poll_trigger_cursor_commit_duration_seconds histogram")
        for i in insts:
            for (op, res), hist in i.cursor_duration.items():
                hist.render(out, f"{p}poll_trigger_cursor_commit_duration_seconds",
                            f'instance="{i.name}",operation="{op}",result="{res}"')

        counter(
            "scheduler_tasks_lease_lost_total",
            "Total number of scheduler tasks whose handler finished after its lease was "
            "reclaimed, by task type.",
            [(f'instance="{i.name}",task_type="{POLL_TASK_TYPE}"', i.lease_lost)
             for i in insts],
        )

        out.append(f"# HELP {p}scheduler_dispatch_lag_seconds "
                   "Delay in seconds between a task becoming due and being dispatched, by task type.")
        out.append(f"# TYPE {p}scheduler_dispatch_lag_seconds histogram")
        for i in insts:
            i.dispatch_lag.render(out, f"{p}scheduler_dispatch_lag_seconds",
                                  f'instance="{i.name}",task_type="{POLL_TASK_TYPE}"')

        return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9102, help="Port to listen on (default: 9102)")
    parser.add_argument("--prefix", default="n8n_", help="Metric name prefix (default: n8n_)")
    parser.add_argument("--instances", type=int, default=1,
                        help="Number of mains to simulate (default: 1). When >1, the last one is degraded.")
    args = parser.parse_args()

    if args.instances < 1:
        parser.error("--instances must be >= 1")

    state = PollTriggerState(args.prefix, args.instances)

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
    print(f"Synthetic n8n poll trigger metrics on http://localhost:{args.port}/metrics "
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
