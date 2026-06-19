# Mocks — webhook traffic generator

Generates realistic demo data for the n8n-webhook-executions Grafana dashboard.

## Setup

Import `mock-webhook-requests-workflow.json` into n8n and **activate** it.
The workflow must be live before running the scripts.

## How failures work

The workflow's **If** node checks for `?fail=true` on incoming requests:
- Present → **Stop and Error** (failed execution)
- Absent → **Edit Fields** sets `status: ok` (successful execution)

The scripts simply append `?fail=true` to a random fraction of requests.

## Scripts

**`generate-webhook-traffic.sh`** — sends a batch of requests and exits.

```
./generate-webhook-traffic.sh [--requests N] [--delay S] [--fail-rate R]
```

**`generate-historic-webhook-traffic.sh`** — loops the above indefinitely to
build up historic data. Pauses 2–10 s (random) between batches. Stop with Ctrl-C.

```
./generate-historic-webhook-traffic.sh [--batch N] [--delay S] [--fail-rate R]
```

Defaults: 50 requests (15 per batch), 0.2 s delay (0.5 s in historic mode), 15% fail rate.
