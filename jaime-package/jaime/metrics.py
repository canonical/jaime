"""Jaime's own metrics, pushed to the opentelemetry-collector.

Jaime has no exporter process or ``/metrics`` server. Instead it pushes OTLP
metric points to the ``opentelemetry-collector`` charm's OTLP/HTTP receiver on
each monitoring cycle; the collector forwards them to Prometheus via
remote_write. This module has no vendor-specific dependency - it emits the OTLP
*JSON* encoding with the standard library only.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from jaime.actions import summarise_usage

logger = logging.getLogger(__name__)

OTELCOL_OTLP_HTTP = "http://127.0.0.1:4318/v1/metrics"
"""The otel-collector OTLP/HTTP metrics receiver on the co-located host."""

_CUMULATIVE = "AGGREGATION_TEMPORALITY_CUMULATIVE"


def _attrs(mapping: dict[str, str]) -> list[dict]:
    """OTLP attribute list from a ``{"key": "value"}`` map."""
    return [{"key": key, "value": {"string_value": value}} for key, value in sorted(mapping.items())]


def _gauge(name: str, value: int, mapping: dict[str, str], now: int) -> dict:
    return {
        "name": name,
        "gauge": {"data_points": [{"time_unix_nano": str(now), "attributes": _attrs(mapping), "as_int": value}]},
    }


def _sum(name: str, value, mapping: dict[str, str], now: int) -> dict:
    point = {"time_unix_nano": str(now), "attributes": _attrs(mapping)}
    if isinstance(value, float):
        point["as_double"] = value
    else:
        point["as_int"] = value
    return {
        "name": name,
        "sum": {
            "data_points": [point],
            "aggregation_temporality": _CUMULATIVE,
            "is_monotonic": True,
        },
    }


def metrics_payload(tracker) -> dict:
    """Build the OTLP/JSON metrics payload from a ``StatusTracker``.

    Same ``jaime_*`` names as the Grafana dashboard queries:
    monitoring gauges per unit and per-model LLM usage cumulative sums.
    """
    now = time.time_ns()
    metrics: list[dict] = []

    # Monitoring gauges.
    for unit, state in tracker.entries():
        incident = state.get("incident") or {}
        is_open = bool(incident) and not incident.get("closed_at")
        unhealthy = bool(state.get("unhealthy_since"))
        metrics.append(_gauge("jaime_monitored_units", 1, {"unit": unit}, now))
        if unhealthy:
            metrics.append(_gauge("jaime_unit_unhealthy", 1, {"unit": unit}, now))
        if is_open:
            metrics.append(_gauge("jaime_incidents_open", 1, {"unit": unit}, now))

    # Per-model AI usage cumulative sums.
    summary = summarise_usage(tracker.all_usage_log(), tracker.folded_usage())
    for model, m in sorted(summary.get("by_model", {}).items()):
        metrics.append(_sum("jaime_llm_calls_total", m["calls"], {"model": model}, now))
        metrics.append(
            _sum(
                "jaime_llm_tokens_total",
                m["prompt_tokens"],
                {"model": model, "role": "prompt"},
                now,
            )
        )
        metrics.append(
            _sum(
                "jaime_llm_tokens_total",
                m["completion_tokens"],
                {"model": model, "role": "completion"},
                now,
            )
        )
        cost = m.get("cost_usd")
        if cost is not None:
            metrics.append(_sum("jaime_llm_cost_total", float(cost), {"model": model}, now))

    resource = {"attributes": [{"key": "juju_application", "value": {"string_value": "jaime"}}]}
    return {
        "resource_metrics": [
            {
                "resource": resource,
                "scope_metrics": [{"scope": {"name": "jaime"}, "metrics": metrics}],
            }
        ]
    }


def push_metrics(tracker, endpoint: str = OTELCOL_OTLP_HTTP) -> None:
    """POST jaime's metrics to the otel-collector's OTLP/HTTP receiver."""
    body = json.dumps(metrics_payload(tracker)).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except OSError as e:  # noqa: BLE001 - network service is best-effort
        logger.debug("metrics push to %s failed: %s", endpoint, e)
