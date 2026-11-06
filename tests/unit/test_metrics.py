"""Unit tests for jaime's OTLP metrics payload builder."""

import datetime

from jaime.metrics import metrics_payload
from jaime.principal import StatusTracker


def _scooped(payload) -> list[dict]:
    return payload["resource_metrics"][0]["scope_metrics"][0]["metrics"]


def _open_incident(unit: str) -> dict:
    return {
        "id": "id-1",
        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status_message": "blocked",
    }


def _seed(tracker, unit: str) -> None:
    tracker.observe(unit, "error", "2026-11-03T00:00:00+00:00", watched=True, message="blocked")


def _attr(points, key):
    for point in points:
        for a in point.get("attributes", []):
            if a["key"] == key:
                return a["value"]["string_value"]
    return None


def test_empty_tracker_emits_no_metric_points(tmp_path):
    tracker = StatusTracker(str(tmp_path / "state.json"))
    assert _scooped(metrics_payload(tracker)) == []


def test_open_incident_emits_monitoring_gauges(tmp_path):
    tracker = StatusTracker(str(tmp_path / "state.json"))
    _seed(tracker, "mysql/0")
    tracker.update_incident("mysql/0", _open_incident("mysql/0"))
    names = [m["name"] for m in _scooped(metrics_payload(tracker))]
    assert "jaime_monitored_units" in names
    assert "jaime_unit_unhealthy" in names
    assert "jaime_incidents_open" in names
    open_ = next(m for m in _scooped(metrics_payload(tracker)) if m["name"] == "jaime_incidents_open")
    assert _attr(open_["gauge"]["data_points"], "unit") == "mysql/0"


def test_closed_incident_emits_no_open_gauge(tmp_path):
    tracker = StatusTracker(str(tmp_path / "state.json"))
    _seed(tracker, "mysql/0")
    incident = _open_incident("mysql/0")
    incident["closed_at"] = "2026-11-03T00:00:00+00:00"
    tracker.update_incident("mysql/0", incident)
    names = [m["name"] for m in _scooped(metrics_payload(tracker))]
    assert "jaime_incidents_open" not in names


def test_usage_emits_cumulative_sums(tmp_path):
    tracker = StatusTracker(str(tmp_path / "state.json"))
    _seed(tracker, "mysql/0")
    tracker.record_usage(
        "mysql/0",
        {
            "model": "deepseek/deepseek-v4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.001,
        },
    )
    metric = _scooped(metrics_payload(tracker))
    names = [m["name"] for m in metric]
    assert "jaime_llm_calls_total" in names
    assert "jaime_llm_cost_total" in names

    tokens = [m for m in metric if m["name"] == "jaime_llm_tokens_total"]
    prompt = next(t for t in tokens if _attr(t["sum"]["data_points"], "role") == "prompt")
    assert prompt["sum"]["data_points"][0]["as_int"] == 100
    assert prompt["sum"]["aggregation_temporality"] == "AGGREGATION_TEMPORALITY_CUMULATIVE"
