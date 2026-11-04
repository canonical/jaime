"""Scenario tests for the machine charm's cos-agent integration."""

from charm import JaimeCharm
from ops.testing import Harness

from jaime.metrics import METRICS_PORT, _escape_label, render_metrics
from jaime.principal import StatusTracker


def test_cos_agent_relation_advertises_metrics_endpoint():
    h = Harness(JaimeCharm)
    h.begin()
    rid = h.add_relation("cos-agent", "grafana-agent")
    h.add_relation_unit(rid, "grafana-agent/0")

    data = h.get_relation_data(rid, h.charm.unit.name) or {}
    body = "\n".join(data.values())
    assert "metrics" in body.lower()
    assert str(METRICS_PORT) in body
    assert "/metrics" in body


def test_metrics_port_module_constant():
    assert METRICS_PORT == 7800


def test_render_uses_tracker(tmp_path):
    tracker = StatusTracker(str(tmp_path / "state.json"))
    tracker.observe("mysql/0", "error", "2026-11-03T00:00:00+00:00", watched=True, message="x")
    tracker.update_incident(
        "mysql/0",
        {"id": "i", "opened_at": "2026-11-03T00:00:00+00:00", "status_message": "x"},
    )
    body = render_metrics(tracker)
    assert 'jaime_incidents_open{unit="mysql/0"} 1' in body
    assert _escape_label('a"b') == 'a\\"b'
