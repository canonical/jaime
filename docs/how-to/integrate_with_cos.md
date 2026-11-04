# Integrate with the Canonical Observability Stack (COS)

Jaime can expose its own incidents and AI usage - tokens, cost, models - to the
Canonical Observability Stack (COS): Prometheus for metrics, Grafana for
dashboards, and Loki for logs. This is optional; without it Jaime works exactly
the same, just without a shared metrics view.

An `opentelemetry-collector` subordinate joins the loop: Jaime pushes its
`jaime_*` metrics to it (OTLP), the collector forwards them to Prometheus, and
the `cos-agent` relation ships a Grafana dashboard.

## Deploy and relate

```bash
# 1. Consume the COS offers (the bundle runs under its own "cos" model)
juju consume cos.loki
juju consume cos.prometheus
juju consume cos.grafana

# 2. Deploy the collector in the same model as Jaime and the principal
juju deploy opentelemetry-collector --base=ubuntu@24.04/stable

# 3. Co-locate it with the principal and with Jaime
juju relate opentelemetry-collector postgresql:cos-agent
juju relate opentelemetry-collector jaime:cos-agent

# 4. Connect it to the COS backends
juju relate opentelemetry-collector cos.grafana
juju relate opentelemetry-collector cos.loki
juju relate opentelemetry-collector cos.prometheus
```

Once the relations settle, open the **Jaime** dashboard in Grafana - it shows
open incidents, monitored and unhealthy units, and per-model LLM tokens and
cost. The metrics are named `jaime_` (for example `jaime_incidents_open`,
`jaime_llm_tokens_total`) and can also be queried directly in Prometheus.

## How it works

- On every monitoring cycle Jaime pushes `jaime_*` metrics (OTLP) to the
  co-located collector, which remote-writes them to Prometheus.
- The `cos-agent` relation ships Jaime's Grafana dashboard to Grafana.
- The same collector can also observe the principal through its own
  `cos-agent` and forward its logs and metrics.

## Prerequisites

- The machines running Jaime and the collector must be able to reach the COS
  endpoints (the addresses Prometheus, Grafana and Loki are exposed on). If
  they sit on separate networks - for example a Kubernetes-based COS and LXD
  machines - add a route on each machine so metrics can flow.
- Jaime provides the `cos-agent` relation; the collector requires it, so the
  relation both co-locates the two and uploads the dashboard.