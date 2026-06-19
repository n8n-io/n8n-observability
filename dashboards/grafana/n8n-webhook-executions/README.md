# n8n Webhook Executions Dashboard

Grafana dashboard for monitoring webhook execution activity in n8n — tracking execution counts, success/failure rates, and latency across all webhook workflows.

## Screenshots

<!-- Screenshots will be added here -->

## Prerequisites

Enable metrics in n8n by setting the following environment variables before starting n8n:

```
N8N_METRICS=true
N8N_METRICS_INCLUDE_WEBHOOK_METRICS=true
```

See the [n8n Prometheus docs](https://docs.n8n.io/hosting/configuration/configuration-examples/prometheus/) for more information.

Verify metrics are exposed by visiting `http://your-n8n-host.com/metrics` — you should see Prometheus-formatted output including `n8n_webhook_*` metrics.

## Import into Grafana

1. Open Grafana and go to **Dashboards → Import**
2. Click **Upload dashboard JSON file**
3. Select [`n8n-webhook-executions.json`](n8n-webhook-executions.json)
4. Select your Prometheus datasource when prompted
5. Click **Import**

## Configuration

### Environment picker

The dashboard has an **Environment** picker at the top that controls where workflow-name links open (e.g. clicking a workflow name opens `<base-url>/workflow/<id>` in n8n).

To add your own environments, edit the `n8n_base_url` variable in `n8n-webhook-executions.json`:

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
  ]
}
```

The `query` field uses Grafana's `Label : value, Label : value` syntax; keep the `options` array in sync.
