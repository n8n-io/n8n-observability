#!/usr/bin/env python3
"""
Synthetic Prometheus exporter for the n8n System Tasks dashboard.

Serves a /metrics endpoint mimicking the `n8n_system_task_*` series, plus the
`n8n_scheduler_*` series of the durable tasks, with moving demo data: the 15
system tasks n8n registers today, a task that stopped being scheduled, a task
that fails and retries, a task with no success recorded yet, skips of all four
reasons, and two tasks running on the durable scheduler.

Real cadences run in minutes and hours, so they are compressed here to tens of
seconds: `system_task_interval_seconds` reports the compressed cadence, which
keeps the overdue factor (age of last success / cadence) honest.

--instances N simulates N mains via an `instance` label (n8n-main-1..N). In-memory
series exist on the leader only (n8n-main-1), which is what the Leaders per Task
panel reads; durable series exist on every main. Requires `honor_labels: true` on
the Prometheus job.

Usage: ./system-task-metrics-exporter.py [--port 9102] [--prefix n8n_] [--instances 1]
Standard library only.
"""

import argparse
import math
import random
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Must match DURATION_BUCKETS_SECONDS and LAG_BUCKETS_SECONDS in
# packages/cli/src/metrics/prometheus/constant.ts so histogram_quantile behaves
# exactly like it does against a real instance.
DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600]
LAG_BUCKETS = [0.01, 0.1, 1, 5, 10, 30, 60, 300, 600, 1800, 3600, 21600, 86400]

# The 15 system tasks n8n registers today, with cadences compressed for the demo
# and a typical run duration in seconds.
#
# Per-task demo behaviour:
#   durable   runs on the durable scheduler instead of an in-memory timer
#   dead      stopped being scheduled, so it never runs again (scheduled == 0)
#   absent    registered but no run yet, as just after a leader takeover
#   fail_rate share of runs that fail
#   skips     {reason: probability} for occurrences that do not run
TASKS = [
    {"name": "jti-cleanup", "interval": 5, "duration": 0.03},
    {"name": "trusted-key-refresh", "interval": 6, "duration": 0.15},
    {"name": "instance-registry-reconciliation", "interval": 8, "duration": 0.08},
    {"name": "instance-registry-stale-member-cleanup", "interval": 10,
     "duration": 0.05, "skips": {"coalesced": 0.06}},
    {"name": "insights-compaction", "interval": 12, "duration": 1.2},
    {"name": "activity-pruning", "interval": 15, "duration": 0.9, "durable": True},
    {"name": "publication-outbox-cleanup", "interval": 18,
     "duration": 0.4, "skips": {"provisioned_elsewhere": 0.3}},
    {"name": "insights-pruning", "interval": 20, "duration": 1.5, "fail_rate": 0.25},
    {"name": "license-renewal", "interval": 25, "duration": 0.6},
    {"name": "agent-checkpoint-pruning", "interval": 30, "duration": 1.1},
    {"name": "execution-pruning-soft-delete", "interval": 30, "duration": 3.5, "durable": True},
    {"name": "instance-ai-checkpoint-pruning", "interval": 40, "duration": 0.7},
    {"name": "workflow-history-compaction-trim", "interval": 45,
     "duration": 20.0, "skips": {"overlap": 0.35, "coalesced": 0.2}},
    {"name": "mcp-registry-refresh", "interval": 60, "duration": 2.0, "dead": True},
    {"name": "workflow-history-compaction-optimize", "interval": 300, "duration": 20.0,
     "absent": True},
]


class Histogram:
    """Cumulative-on-render Prometheus histogram over a fixed bucket list."""

    def __init__(self, buckets):
        self.buckets = buckets
        self.counts = [0.0] * len(buckets)
        self.inf = 0.0
        self.sum = 0.0

    def observe(self, value: float):
        self.sum += value
        for i, le in enumerate(self.buckets):
            if value <= le:
                self.counts[i] += 1
                return
        self.inf += 1

    @property
    def total(self) -> float:
        return sum(self.counts) + self.inf

    def render(self, out, name: str, labels: str):
        cumulative = 0.0
        for i, le in enumerate(self.buckets):
            cumulative += self.counts[i]
            out.append(f'{name}_bucket{{{labels},le="{le}"}} {cumulative:.4f}')
        out.append(f'{name}_bucket{{{labels},le="+Inf"}} {self.total:.4f}')
        out.append(f"{name}_sum{{{labels}}} {self.sum:.4f}")
        out.append(f"{name}_count{{{labels}}} {self.total:.4f}")


# A run in progress: its effects land at `end`, not when it started.
Run = namedtuple("Run", "end instance result duration retried")


class TaskState:
    """One system task's timeline, fired occurrence by occurrence from real elapsed time."""

    def __init__(self, spec: dict, instances: list, wall_start: float):
        self.name = spec["name"]
        self.interval = float(spec["interval"])
        self.duration = float(spec["duration"])
        self.durable = spec.get("durable", False)
        self.dead = spec.get("dead", False)
        self.absent = spec.get("absent", False)
        self.fail_rate = spec.get("fail_rate", 0.0)
        self.skips = spec.get("skips", {})
        self.mode = "durable" if self.durable else "in_memory"

        self.instances = instances
        self.owners = instances if self.durable else instances[:1]
        self.next_owner = 0

        # A dead task has one recorded success from before it stopped being
        # scheduled, so its overdue factor climbs instead of reading as absent.
        seeded = None if self.absent else wall_start
        self.last_success = {i: seeded for i in self.owners}

        self.runs = {i: {} for i in self.owners}          # instance -> (result) -> Histogram
        self.in_flight = []                               # Runs started but not settled yet
        self.skipped = {i: {} for i in self.owners}       # instance -> reason -> count
        self.retries = {i: 0.0 for i in self.owners}
        self.provision_check_failures = {i: 0.0 for i in self.owners}
        self.fire_lag = {i: Histogram(LAG_BUCKETS) for i in self.owners}

        # Durable tasks also show up in the scheduler engine's own metrics.
        self.dispatched = {i: 0.0 for i in self.owners}
        self.dispatch_lag = {i: Histogram(DURATION_BUCKETS) for i in self.owners}

        self.next_fire = wall_start + random.uniform(0, self.interval)

    def _owner(self) -> str:
        owner = self.owners[self.next_owner % len(self.owners)]
        self.next_owner += 1
        return owner

    def _run_histogram(self, instance: str, result: str) -> Histogram:
        return self.runs[instance].setdefault(result, Histogram(DURATION_BUCKETS))

    def advance(self, wall: float):
        # A dead task keeps the occurrence it was armed for when it stopped being
        # scheduled, so its next-run countdown falls past zero instead of moving.
        if not self.dead:
            while self.next_fire <= wall:
                if not self.absent:
                    self._fire(self.next_fire)
                self.next_fire += self.interval
        self._settle(wall)

    def _fire(self, due: float):
        instance = self._owner()

        if not self.durable:
            lag = abs(random.gauss(0.02, 0.02)) + (random.expovariate(1 / 8.0)
                                                   if random.random() < 0.03 else 0.0)
            self.fire_lag[instance].observe(lag)
        else:
            self.dispatched[instance] += 1
            self.dispatch_lag[instance].observe(abs(random.gauss(0.08, 0.06)))

        for reason, probability in self.skips.items():
            if random.random() < probability:
                counts = self.skipped[instance]
                counts[reason] = counts.get(reason, 0.0) + 1
                return

        if random.random() < 0.02:
            counts = self.skipped[instance]
            counts["aborted"] = counts.get("aborted", 0.0) + 1
            return

        duration = max(0.001, random.lognormvariate(math.log(self.duration), 0.5))
        if random.random() < self.fail_rate:
            result = "failure"
            retried = random.random() < 0.8
        elif random.random() < 0.006:
            result = "aborted"
            retried = False
        else:
            result = "success"
            retried = False

        self.in_flight.append(Run(due + duration, instance, result, duration, retried))

        if not self.durable and random.random() < 0.002:
            self.provision_check_failures[instance] += 1

    def _settle(self, wall: float):
        # A run is only finished once its duration has elapsed. Recording its
        # outcome at fire time would count it as in flight and as finished at the
        # same time, and would date its success in the future.
        finished = [run for run in self.in_flight if run.end <= wall]
        self.in_flight = [run for run in self.in_flight if run.end > wall]
        for run in sorted(finished, key=lambda run: run.end):
            self._run_histogram(run.instance, run.result).observe(run.duration)
            if run.result == "success":
                self.last_success[run.instance] = run.end
            if run.retried:
                self.retries[run.instance] += 1

    def running_now(self) -> int:
        return len(self.in_flight)


class SystemTaskState:
    def __init__(self, prefix: str, instance_count: int):
        self.prefix = prefix
        self.wall_start = time.time()
        self.instances = [f"n8n-main-{i + 1}" for i in range(instance_count)]
        self.tasks = [TaskState(spec, self.instances, self.wall_start) for spec in TASKS]

    def render(self) -> str:
        wall = time.time()
        for task in self.tasks:
            task.advance(wall)

        p = self.prefix
        out = []

        def header(name, help_text, metric_type):
            out.append(f"# HELP {p}{name} {help_text}")
            out.append(f"# TYPE {p}{name} {metric_type}")

        header("system_task_info",
               "Always 1 for every system task this instance can run, by task and mode: "
               "durable tasks on every main, in-memory tasks on the leader.", "gauge")
        for t in self.tasks:
            for i in t.owners:
                out.append(f'{p}system_task_info{{instance="{i}",task="{t.name}",'
                           f'mode="{t.mode}"}} 1')

        header("system_task_scheduled",
               "1 while a system task is scheduled to run on this instance, 0 once it stopped "
               "being scheduled, by task and mode: its in-memory schedule could not be planned, "
               "or its durable job could not be provisioned.", "gauge")
        for t in self.tasks:
            for i in t.owners:
                out.append(f'{p}system_task_scheduled{{instance="{i}",task="{t.name}",'
                           f'mode="{t.mode}"}} {0 if t.dead else 1}')

        header("system_task_interval_seconds",
               "Declared cadence in seconds of a system task on an interval schedule, by task.",
               "gauge")
        for t in self.tasks:
            for i in self.instances:
                out.append(f'{p}system_task_interval_seconds{{instance="{i}",'
                           f'task="{t.name}"}} {t.interval:.4f}')

        header("system_task_next_run_timestamp_seconds",
               "Unix timestamp in seconds of the next occurrence an in-memory system task is "
               "armed for on this instance, by task.", "gauge")
        for t in self.tasks:
            if t.durable:
                continue
            for i in t.owners:
                out.append(f'{p}system_task_next_run_timestamp_seconds{{instance="{i}",'
                           f'task="{t.name}"}} {t.next_fire:.4f}')

        header("system_task_runs_in_flight",
               "Number of system task runs currently in flight on this instance, by task and mode.",
               "gauge")
        for t in self.tasks:
            running = t.running_now()
            for idx, i in enumerate(t.owners):
                share = running // len(t.owners) + (1 if idx < running % len(t.owners) else 0)
                out.append(f'{p}system_task_runs_in_flight{{instance="{i}",task="{t.name}",'
                           f'mode="{t.mode}"}} {share}')

        header("system_task_last_success_timestamp_seconds",
               "Unix timestamp in seconds of the last successful run of a system task on this "
               "instance, by task and mode.", "gauge")
        for t in self.tasks:
            for i in t.owners:
                stamp = t.last_success[i]
                if stamp is not None:
                    out.append(f'{p}system_task_last_success_timestamp_seconds{{instance="{i}",'
                               f'task="{t.name}",mode="{t.mode}"}} {stamp:.4f}')

        header("system_task_run_duration_seconds",
               "Duration in seconds of a system task run, by task, mode (in_memory, durable) "
               "and result (success, failure, aborted).", "histogram")
        for t in self.tasks:
            for i in t.owners:
                for result, hist in t.runs[i].items():
                    hist.render(out, f"{p}system_task_run_duration_seconds",
                                f'instance="{i}",task="{t.name}",mode="{t.mode}",'
                                f'result="{result}"')

        header("system_task_runs_skipped_total",
               "Total number of in-memory system task occurrences that did not run, by task and "
               "reason (overlap, provisioned_elsewhere, aborted, coalesced).", "counter")
        for t in self.tasks:
            for i in t.owners:
                for reason, count in t.skipped[i].items():
                    out.append(f'{p}system_task_runs_skipped_total{{instance="{i}",'
                               f'task="{t.name}",reason="{reason}"}} {count:.4f}')

        header("system_task_retries_total",
               "Total number of in-memory system task retries scheduled after a failed run, "
               "by task.", "counter")
        for t in self.tasks:
            for i in t.owners:
                if t.retries[i]:
                    out.append(f'{p}system_task_retries_total{{instance="{i}",'
                               f'task="{t.name}"}} {t.retries[i]:.4f}')

        header("system_task_provision_check_failures_total",
               "Total number of times the check for the durable job of a system task failed, "
               "so the task ran in memory anyway, by task.", "counter")
        for t in self.tasks:
            for i in t.owners:
                if t.provision_check_failures[i]:
                    out.append(f'{p}system_task_provision_check_failures_total{{instance="{i}",'
                               f'task="{t.name}"}} {t.provision_check_failures[i]:.4f}')

        header("system_task_fire_lag_seconds",
               "Delay in seconds between an in-memory system task occurrence being due and its "
               "timer firing, by task.", "histogram")
        for t in self.tasks:
            if t.durable:
                continue
            for i in t.owners:
                if t.fire_lag[i].total:
                    t.fire_lag[i].render(out, f"{p}system_task_fire_lag_seconds",
                                         f'instance="{i}",task="{t.name}"')

        self._render_engine(out)
        return "\n".join(out) + "\n"

    def _render_engine(self, out):
        """The durable scheduler's own series for the tasks it owns, as `system:<name>`."""
        p = self.prefix
        durable = [t for t in self.tasks if t.durable]

        out.append(f"# HELP {p}scheduler_tasks_dispatched_total "
                   "Total number of scheduler tasks dispatched to a handler by task type.")
        out.append(f"# TYPE {p}scheduler_tasks_dispatched_total counter")
        for t in durable:
            for i in t.owners:
                out.append(f'{p}scheduler_tasks_dispatched_total{{instance="{i}",'
                           f'task_type="system:{t.name}"}} {t.dispatched[i]:.4f}')

        out.append(f"# HELP {p}scheduler_tasks_completed_total "
                   "Total number of scheduler tasks that finished firing by task type and result.")
        out.append(f"# TYPE {p}scheduler_tasks_completed_total counter")
        for t in durable:
            for i in t.owners:
                for result, hist in t.runs[i].items():
                    out.append(f'{p}scheduler_tasks_completed_total{{instance="{i}",'
                               f'task_type="system:{t.name}",result="{result}"}} '
                               f"{hist.total:.4f}")

        out.append(f"# HELP {p}scheduler_dispatch_lag_seconds "
                   "Delay in seconds between a task becoming due and being dispatched, "
                   "by task type.")
        out.append(f"# TYPE {p}scheduler_dispatch_lag_seconds histogram")
        for t in durable:
            for i in t.owners:
                t.dispatch_lag[i].render(out, f"{p}scheduler_dispatch_lag_seconds",
                                         f'instance="{i}",task_type="system:{t.name}"')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9102, help="Port to listen on (default: 9102)")
    parser.add_argument("--prefix", default="n8n_", help="Metric name prefix (default: n8n_)")
    parser.add_argument("--instances", type=int, default=1,
                        help="Number of mains to simulate (default: 1). Only the first leads.")
    args = parser.parse_args()

    if args.instances < 1:
        parser.error("--instances must be >= 1")

    state = SystemTaskState(args.prefix, args.instances)

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
    print(f"Synthetic n8n system task metrics on http://localhost:{args.port}/metrics "
          f"(prefix={args.prefix!r})")
    print(f"Simulating {args.instances} main(s), leader: {state.instances[0]}")
    if args.instances > 1:
        print("Remember: Prometheus must scrape this target with `honor_labels: true`.")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
