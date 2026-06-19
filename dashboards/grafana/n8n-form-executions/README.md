# n8n Form Executions Dashboard

Grafana dashboard for monitoring form execution activity in n8n — tracking execution counts and success/failure rates across all form workflows.

## Screenshots

<!-- Screenshots will be added here -->

## Prerequisites

Enable metrics in n8n by setting the following environment variables before starting n8n:

```
N8N_METRICS=true
N8N_METRICS_INCLUDE_FORM_METRICS=true
```

See the [n8n Prometheus docs](https://docs.n8n.io/hosting/configuration/configuration-examples/prometheus/) for more information.

Verify metrics are exposed by visiting `http://your-n8n-host.com/metrics` — you should see Prometheus-formatted output including `n8n_form_*` metrics.

## Import into Grafana

1. Open Grafana and go to **Dashboards → Import**
2. Click **Upload dashboard JSON file**
3. Select [`n8n-form-executions.json`](n8n-form-executions.json)
4. Select your Prometheus datasource when prompted
5. Click **Import**
