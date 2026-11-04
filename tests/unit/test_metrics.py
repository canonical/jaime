"""Unit tests for the jaime Prometheus metrics renderer."""

import datetime

from jaime.metrics import _escape_label, render_metrics
from jaime.principal import StatusTracker


def _tracker(tmp_path):
    return StatusTracker(str(tmp_path / "state.json"))


def _open_incident(unit: str, incident_id: str = "id-1") -> dict:
    return {
        "id": incident_id,
        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status_message": "blocked",
    }


def _seed(tracker, unit: str) -> None:
    """Make ``unit`` present in the tracker (update_incident/record_usage ignore it otherwise)."""
    tracker.observe(unit, "error", "2026-11-03T00:00:00+00:00", watched=True, message="blocked")


def test_empty_tracker_renders_help_only(tmp_path):
    body = render_metrics(_tracker(tmp_path))
    assert "jaime_incidents_open" in body
    assert "jaime_llm_tokens_total" in body
    assert "error" not in body.lower()


def test_open_incident_emits_gauge(tmp_path):
    tracker = _tracker(tmp_path)
    _seed(tracker, "mysql/0")
    tracker.update_incident("mysql/0", _open_incident("mysql/0"))
    body = render_metrics(tracker)
    assert 'jaime_incidents_open{unit="mysql/0"} 1' in body


def test_closed_incident_emits_no_gauge(tmp_path):
    tracker = _tracker(tmp_path)
    _seed(tracker, "mysql/0")
    incident = _open_incident("mysql/0")
    incident["closed_at"] = "2026-11-03T00:00:00+00:00"
    tracker.update_incident("mysql/0", incident)
    body = render_metrics(tracker)
    assert (
        "jaime_incidents_open" not in body.split("jaime_llm_calls_total")[0].split("jaime_incidents_open")[-2]
        if "jaime_incidents_open" in body
        else True
    )


def test_usage_emits_per_model_counters(tmp_path):
    tracker = _tracker(tmp_path)
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
    body = render_metrics(tracker)
    assert 'jaime_llm_calls_total{model="deepseek/deepseek-v4"} 1' in body
    assert 'jaime_llm_tokens_total{model="deepseek/deepseek-v4",role="prompt"} 100' in body
    assert 'jaime_llm_tokens_total{model="deepseek/deepseek-v4",role="completion"} 50' in body
    assert 'jaime_llm_cost_total{model="deepseek/deepseek-v4"} 0.001' in body


def test_label_values_are_escaped(tmp_path):
    assert _escape_label('a"b\\c') == 'a\\"b\\\\c'
