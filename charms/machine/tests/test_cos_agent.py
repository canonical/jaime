"""Scenario tests for the machine charm's cos-agent integration."""

import json
import pathlib

import pytest
from charm import JaimeCharm
from ops.testing import Harness


def test_instantiates_with_cos_agent_relation():
    h = Harness(JaimeCharm)
    h.begin()
    rid = h.add_relation("cos-agent", "grafana-agent")
    h.add_relation_unit(rid, "grafana-agent/0")
    assert h.model.get_relation("cos-agent") is not None


@pytest.mark.parametrize("key", ["jaime_incidents_open", "jaime_llm_tokens_total"])
def test_dashboard_panels_query_jaime_metrics(key):
    payload = json.loads(pathlib.Path("src/grafana_dashboards/jaime.json").read_text())
    body = json.dumps(payload)
    assert payload["title"] == "Jaime"
    assert key in body
