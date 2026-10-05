"""Unit tests for jaime.core (shared charm logic)."""

import json
import unittest.mock as mock

from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from jaime.core import CoreMixin, Mode, Provider, summarise_usage
from jaime.incident import Incident
from jaime.logging import list_incidents
from jaime.principal import StatusTracker


class _DummyCharm(CoreMixin, CharmBase):
    """Minimal charm used to exercise the shared CoreMixin behaviour."""

    def __init__(self, *args):
        super().__init__(*args)
        self._status_tracker = StatusTracker()
        self.framework.observe(self.on.config_changed, self._on_config_changed)


# Declared inline rather than read from either charm's config.yaml. CoreMixin
# is substrate-independent, so its test must not depend on a particular
# charm's packaging; this also keeps the suite runnable from the repo root.
# Only the options CoreMixin itself reads are listed.
_SHARED_CONFIG_YAML = """
options:
  mode:
    type: string
    default: observe
  provider:
    type: string
    default: none
  model:
    type: string
    default: ""
  api-token:
    type: string
    default: ""
  watch-statuses:
    type: string
    default: "error,blocked"
  failure-timeout-minutes:
    type: int
    default: 5
  cooldown-minutes:
    type: int
    default: 30
  report-dir:
    type: string
    default: /var/log/jaime/reports
  audit-log-path:
    type: string
    default: /var/log/jaime/events.jsonl
"""


def _make_harness(config_overrides=None):
    h = Harness(_DummyCharm, config=_SHARED_CONFIG_YAML)
    h.begin()
    if config_overrides:
        h.update_config(config_overrides)
    return h


class TestConfigChanged:
    def test_invalid_mode_sets_blocked(self):
        h = _make_harness({"mode": "diagnose"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "invalid mode" in h.charm.unit.status.message

    def test_observe_mode_sets_ready(self):
        h = _make_harness({"mode": "observe"})
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"

    def test_invalid_provider_sets_blocked(self):
        h = _make_harness({"mode": "observe", "provider": "foobar"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "invalid provider" in h.charm.unit.status.message

    def test_act_mode_sets_blocked_not_implemented(self):
        h = _make_harness({"mode": "act"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "not yet implemented" in h.charm.unit.status.message

    def test_suggest_mode_no_provider_sets_blocked(self):
        h = _make_harness({"mode": "suggest", "provider": "none"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "provider is not configured" in h.charm.unit.status.message

    def test_valid_provider_and_token_sets_active(self):
        h = _make_harness({"mode": "suggest", "provider": "gemini", "api-token": "tok"})
        mock_provider = mock.MagicMock()
        mock_provider.check.return_value = None
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(mock_provider, None)):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)

    def test_bad_token_sets_blocked(self):
        h = _make_harness({"mode": "suggest", "provider": "gemini", "api-token": "bad"})
        mock_provider = mock.MagicMock()
        mock_provider.check.return_value = "HTTP 401: invalid"
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(mock_provider, None)):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "AI provider error" in h.charm.unit.status.message

    def test_config_change_preserves_open_incident_status(self):
        """A config change must not overwrite an open incident's status."""
        h = _make_harness({"mode": "observe"})
        inc = Incident.open()
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "increment": 3,
            "incident": inc.to_dict(),
            "last_reported": "2026-01-01T00:01:00+00:00",
        }
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(None, "no provider")):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "incident open" in h.charm.unit.status.message
        assert inc.id[:8] in h.charm.unit.status.message

    def test_config_change_no_incident_sets_ready(self):
        h = _make_harness({"mode": "observe"})
        h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"


class TestShowStatus:
    def _run(self, h, monitored=None):
        event = mock.MagicMock()
        with mock.patch.object(h.charm, "_monitored_applications",
                               return_value=monitored or []):
            h.charm._on_action_show_status(event)
        return json.loads(event.set_results.call_args[0][0]["result"])

    def test_empty_state_returns_empty_list(self):
        h = _make_harness()
        assert self._run(h) == []

    def test_returns_every_tracked_unit(self):
        h = _make_harness()
        inc = Incident.open()
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "unhealthy_since": "2026-01-01T00:00:00+00:00",
            "increment": 3,
            "incident": inc.to_dict(),
            "last_reported": "2026-01-01T00:01:00+00:00",
        }
        h.charm._status_tracker._state["mysql/0"] = {
            "status": "active",
            "since": "2026-01-01T00:02:00+00:00",
            "increment": 1,
        }

        records = self._run(h, ["postgresql", "mysql"])
        assert [r["unit"] for r in records] == ["postgresql/0", "mysql/0"]
        by_unit = {r["unit"]: r for r in records}
        assert by_unit["postgresql/0"]["workload"] == "blocked"
        assert by_unit["postgresql/0"]["incident-id"] == inc.id
        assert by_unit["postgresql/0"]["first-seen"] == "2026-01-01T00:00:00+00:00"
        assert by_unit["postgresql/0"]["increment"] == 3
        assert by_unit["mysql/0"]["incident-id"] == ""
        assert by_unit["mysql/0"]["increment"] == 1


class TestEnums:
    def test_mode_values(self):
        assert Mode.OBSERVE.value == "observe"
        assert Mode.SUGGEST.value == "suggest"
        assert Mode.ACT.value == "act"

    def test_provider_values(self):
        assert Provider.NONE.value == "none"
        assert Provider.GEMINI.value == "gemini"
        assert Provider.OPENROUTER.value == "openrouter"


class TestSummariseUsage:
    def test_empty(self):
        assert summarise_usage([]) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": None,
            "by_model": {},
        }

    def test_rolls_up_single_entry(self):
        result = summarise_usage([{
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.001, "model": "deepseek/deepseek-chat",
        }])
        assert result["total_tokens"] == 150
        assert result["cost_usd"] == 0.001
        assert result["by_model"]["deepseek/deepseek-chat"]["calls"] == 1

    def test_breaks_down_by_model(self):
        result = summarise_usage([
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "cost_usd": 0.001, "model": "m1"},
            {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
             "cost_usd": 0.002, "model": "m2"},
        ])
        assert result["total_tokens"] == 45
        assert result["by_model"]["m1"]["total_tokens"] == 15
        assert result["by_model"]["m2"]["total_tokens"] == 30
        assert abs(result["cost_usd"] - 0.003) < 1e-9

    def test_missing_cost_yields_none(self):
        result = summarise_usage([{
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
            "model": "gemini-2.5-flash",
        }])
        assert result["cost_usd"] is None
        assert result["by_model"]["gemini-2.5-flash"]["cost_usd"] is None


class TestResolveSecret:
    def _make(self, model):
        charm = object.__new__(CoreMixin)
        charm.model = model
        return charm

    def test_plain_value_returned(self):
        model = mock.MagicMock()
        model.config.get.return_value = "plain-token"
        charm = self._make(model)
        assert charm._resolve_api_token() == "plain-token"

    def test_secret_resolution(self):
        model = mock.MagicMock()
        model.config.get.return_value = "secret:abc123"
        secret = mock.MagicMock()
        secret.get_content.return_value = {"token": "the-token"}
        model.get_secret.return_value = secret
        charm = self._make(model)
        assert charm._resolve_api_token() == "the-token"

    def test_secret_error_returns_empty(self):
        model = mock.MagicMock()
        model.config.get.return_value = "secret:abc123"
        model.get_secret.side_effect = Exception("denied")
        charm = self._make(model)
        assert charm._resolve_api_token() == ""


class TestGetAIProvider:
    def _make(self, config: dict):
        model = mock.MagicMock()
        model.config.get.side_effect = lambda key, default=None: config.get(key, default)
        charm = object.__new__(CoreMixin)
        charm.model = model
        return charm

    def test_provider_none(self):
        charm = self._make({"provider": "none"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "provider is not configured" in err

    def test_missing_token(self):
        charm = self._make({"provider": "gemini", "api-token": ""})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "api-token is not set" in err

    def test_unsupported_provider(self):
        charm = self._make({"provider": "foo"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "unsupported provider" in err

    def test_gemini_provider(self):
        charm = self._make({"provider": "gemini", "api-token": "tok"})
        provider, err = charm._get_ai_provider()
        from jaime.providers.gemini import GeminiProvider
        assert isinstance(provider, GeminiProvider)
        assert err is None

    def test_default_model(self):
        charm = self._make({})
        assert charm._default_model("gemini") == "gemini-2.5-flash"
        assert charm._default_model("openrouter") == "~deepseek/deepseek-v4-flash-latest"


class TestReadyMessage:
    def test_default_is_plain_ready(self):
        h = _make_harness()
        assert h.charm._ready_message() == "Ready"

    def test_names_monitored_applications(self):
        h = _make_harness()
        with mock.patch.object(h.charm, "_monitored_applications",
                               return_value=["postgresql", "redis"]):
            assert h.charm._ready_message() == "Ready: monitoring postgresql, redis"


class TestShowStatusFilter:
    def test_filters_to_monitored_applications(self):
        """An application no longer monitored disappears from the action."""
        h = _make_harness()
        h.charm._status_tracker._state = {
            "a/0": {"status": "active", "increment": 1},
            "b/0": {"status": "active", "increment": 1},
        }
        event = mock.MagicMock()
        with mock.patch.object(h.charm, "_monitored_applications", return_value=["a"]):
            h.charm._on_action_show_status(event)
        records = json.loads(event.set_results.call_args[0][0]["result"])
        assert [r["unit"] for r in records] == ["a/0"]

    def test_record_includes_status_message(self):
        h = _make_harness()
        h.charm._status_tracker._state = {
            "a/0": {
                "status": "blocked",
                "message": "Please initialize OpenBao or integrate with an auto-unseal provider",
                "increment": 1,
            },
        }
        event = mock.MagicMock()
        with mock.patch.object(h.charm, "_monitored_applications", return_value=["a"]):
            h.charm._on_action_show_status(event)
        records = json.loads(event.set_results.call_args[0][0]["result"])
        assert records[0]["status-message"] == (
            "Please initialize OpenBao or integrate with an auto-unseal provider"
        )

    def test_record_status_message_defaults_empty(self):
        h = _make_harness()
        h.charm._status_tracker._state = {"a/0": {"status": "active", "increment": 1}}
        event = mock.MagicMock()
        with mock.patch.object(h.charm, "_monitored_applications", return_value=["a"]):
            h.charm._on_action_show_status(event)
        records = json.loads(event.set_results.call_args[0][0]["result"])
        assert records[0]["status-message"] == ""


class TestListIncidentsAction:
    def _write_events(self, path, *events):
        from jaime.logging import write_event
        for e in events:
            write_event(e, str(path))

    def test_returns_incidents_and_unit_filter(self, tmp_path):
        events = str(tmp_path / "events.jsonl")
        self._write_events(
            events,
            {"event": "incident-start", "unit": "a/0", "incident_id": "i1",
             "status_message": "blocked: no leader", "timestamp": "2026-07-13T00:00:00+00:00"},
            {"event": "incident-start", "unit": "b/0", "incident_id": "i2",
             "timestamp": "2026-07-14T00:00:00+00:00"},
        )
        h = _make_harness({"audit-log-path": events})
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_list_incidents(event)
        records = json.loads(event.set_results.call_args[0][0]["result"])
        assert [r["incident_id"] for r in records] == ["i2", "i1"]
        by_id = {r["incident_id"]: r for r in records}
        assert by_id["i1"]["status_message"] == "blocked: no leader"
        assert by_id["i2"]["status_message"] == ""

        event2 = mock.MagicMock()
        event2.params = {"unit": "a/0"}
        h.charm._on_action_list_incidents(event2)
        records2 = json.loads(event2.set_results.call_args[0][0]["result"])
        assert [r["incident_id"] for r in records2] == ["i1"]

    def test_missing_log_returns_empty(self, tmp_path):
        h = _make_harness({"audit-log-path": str(tmp_path / "missing.jsonl")})
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_list_incidents(event)
        records = json.loads(event.set_results.call_args[0][0]["result"])
        assert records == []


class TestIncidentClosedAudit:
    """incident-closed must be durably written to events.jsonl."""

    def _archives(self, tmp_path):
        return sorted(tmp_path.glob("events.jsonl.*"))

    def test_reset_writes_incident_closed(self, tmp_path):
        from jaime.incident import Incident
        h = _make_harness({"audit-log-path": str(tmp_path / "events.jsonl")})
        inc = Incident.open()
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "increment": 3, "incident": inc.to_dict(),
            "last_reported": "2026-01-01T00:01:00+00:00",
        }
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        import json as _json
        # The closure row lands in the rotated archive; the main log is fresh.
        archives = self._archives(tmp_path)
        assert len(archives) == 1
        closed = [_json.loads(line) for line in archives[0].read_text().splitlines()]
        assert len(closed) == 1
        assert closed[0]["event"] == "incident-closed"
        assert closed[0]["incident_id"] == inc.id
        assert closed[0]["closed_at"] is not None
        assert closed[0]["reason"] == "manual reset"

    def test_reset_rotates_audit_log(self, tmp_path):
        from jaime.logging import write_event
        audit = str(tmp_path / "events.jsonl")
        write_event({"event": "incident-start", "unit": "a/0", "incident_id": "i1",
                     "timestamp": "2026-10-04T20:09:03+00:00"}, audit)
        h = _make_harness({"audit-log-path": audit})
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        # Main log exists (fresh/empty); history is in the archive.
        assert tmp_path.joinpath("events.jsonl").exists()
        assert list_incidents(audit) == []
        archives = self._archives(tmp_path)
        assert len(archives) == 1

    def test_recovery_writes_incident_closed(self, tmp_path):
        from jaime.incident import Incident
        h = _make_harness({"audit-log-path": str(tmp_path / "events.jsonl")})
        inc = Incident.open()
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "unhealthy_since": "2026-01-01T00:00:00+00:00",
            "increment": 1, "incident": inc.to_dict(),
        }
        # Healthy status (not in watch-statuses) triggers the recovery branch.
        h.charm._process_unit("postgresql/0", "active", "2026-01-01T00:02:00+00:00")
        import json as _json
        lines = open(tmp_path / "events.jsonl").read().splitlines()
        closed = [_json.loads(line) for line in lines]
        assert len(closed) == 1
        assert closed[0]["event"] == "incident-closed"
        assert closed[0]["incident_id"] == inc.id
        assert closed[0]["closed_at"] is not None
        assert "reason" not in closed[0]
        # Recovery does not rotate.
        assert not (tmp_path / "events.jsonl.0").exists() and not self._archives(tmp_path)

    def test_reset_closes_audit_incidents_not_in_tracker(self, tmp_path):
        """Reset must close incidents that only survive in the audit log."""
        from jaime.logging import write_event
        audit = str(tmp_path / "events.jsonl")
        write_event({
            "event": "incident-start", "unit": "mysql/0", "incident_id": "old-id",
            "timestamp": "2026-10-04T20:09:03+00:00",
        }, audit)
        h = _make_harness({"audit-log-path": audit})
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        import json as _json
        archives = self._archives(tmp_path)
        assert len(archives) == 1
        closed = [_json.loads(line) for line in archives[0].read_text().splitlines()
                  if _json.loads(line)["event"] == "incident-closed"]
        assert len(closed) == 1
        assert closed[0]["incident_id"] == "old-id"
        assert closed[0]["unit"] == "mysql/0"
        assert closed[0]["closed_at"] is not None

    def test_reset_writes_single_closure_row_per_incident(self, tmp_path):
        """A tracked incident that is also in the audit log gets one row only."""
        from jaime.incident import Incident
        from jaime.logging import write_event
        audit = str(tmp_path / "events.jsonl")
        inc = Incident.open()
        write_event({
            "event": "incident-start", "unit": "postgresql/0",
            "incident_id": inc.id, "timestamp": "2026-01-01T00:00:00+00:00",
        }, audit)
        h = _make_harness({"audit-log-path": audit})
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "increment": 3, "incident": inc.to_dict(),
        }
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        import json as _json
        archives = self._archives(tmp_path)
        assert len(archives) == 1
        closed = [_json.loads(line) for line in archives[0].read_text().splitlines()
                  if _json.loads(line)["event"] == "incident-closed"]
        assert len(closed) == 1
        assert closed[0]["incident_id"] == inc.id
