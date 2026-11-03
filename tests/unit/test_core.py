"""Unit tests for jaime.core (shared charm logic)."""

import json
import os
import unittest.mock as mock

import pytest
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from jaime.actions import summarise_usage
from jaime.core import CoreMixin
from jaime.incident import Incident, Suggestion, Turn
from jaime.logging import list_incidents
from jaime.modes import Mode, Provider
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
  provider-remote-url:
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
  max-prompt-bytes:
    type: int
    default: 0
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

    def test_provider_remote_url_with_non_remote_provider_sets_blocked(self):
        h = _make_harness({"provider": "gemini", "provider-remote-url": "http://x:11434/v1"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "provider-remote-url is set but provider is not 'remote'" in h.charm.unit.status.message

    def test_provider_remote_url_with_remote_provider_is_active(self):
        h = _make_harness(
            {
                "mode": "suggest",
                "provider": "remote",
                "provider-remote-url": "http://x:11434/v1",
                "model": "qwen2.5:7b",
            }
        )
        mock_provider = mock.MagicMock()
        mock_provider.check.return_value = None
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(mock_provider, None)):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)

    def test_provider_remote_url_with_none_provider_is_ignored(self):
        """provider=none uses no provider, so a leftover URL is not an error."""
        h = _make_harness({"provider": "none", "provider-remote-url": "http://x:11434/v1"})
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"

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
        with mock.patch.object(h.charm, "_monitored_applications", return_value=monitored or []):
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
        assert Provider.REMOTE.value == "remote"


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
        result = summarise_usage(
            [
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cost_usd": 0.001,
                    "model": "deepseek/deepseek-chat",
                }
            ]
        )
        assert result["total_tokens"] == 150
        assert result["cost_usd"] == 0.001
        assert result["by_model"]["deepseek/deepseek-chat"]["calls"] == 1

    def test_breaks_down_by_model(self):
        result = summarise_usage(
            [
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": 0.001, "model": "m1"},
                {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost_usd": 0.002, "model": "m2"},
            ]
        )
        assert result["total_tokens"] == 45
        assert result["by_model"]["m1"]["total_tokens"] == 15
        assert result["by_model"]["m2"]["total_tokens"] == 30
        assert abs(result["cost_usd"] - 0.003) < 1e-9

    def test_missing_cost_yields_none(self):
        result = summarise_usage(
            [
                {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "model": "gemini-2.5-flash",
                }
            ]
        )
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
        assert charm._default_model("remote") == ""

    def test_remote_requires_remote_url(self):
        charm = self._make({"provider": "remote", "model": "llama3.2"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "provider-remote-url is not set" in err

    def test_remote_requires_model(self):
        charm = self._make({"provider": "remote", "provider-remote-url": "http://h:11434/v1"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "model is not set" in err

    def test_remote_allows_empty_token(self):
        charm = self._make(
            {
                "provider": "remote",
                "provider-remote-url": "http://host:11434/v1",
                "model": "llama3.2",
            }
        )
        provider, err = charm._get_ai_provider()
        from jaime.providers.remote import RemoteProvider

        assert isinstance(provider, RemoteProvider)
        assert err is None
        assert provider._api_token == ""
        assert provider._base_url == "http://host:11434/v1"
        assert provider._model == "llama3.2"

    def test_remote_with_token(self):
        charm = self._make(
            {
                "provider": "remote",
                "provider-remote-url": "http://host:11434/v1",
                "model": "llama3.2",
                "api-token": "tok",
            }
        )
        provider, err = charm._get_ai_provider()
        assert err is None
        assert provider._api_token == "tok"


class TestReadyMessage:
    def test_default_is_plain_ready(self):
        h = _make_harness()
        assert h.charm._ready_message() == "Ready"

    def test_names_monitored_applications(self):
        h = _make_harness()
        with mock.patch.object(h.charm, "_monitored_applications", return_value=["postgresql", "redis"]):
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
        assert records[0]["status-message"] == ("Please initialize OpenBao or integrate with an auto-unseal provider")

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
            {
                "event": "incident-start",
                "unit": "a/0",
                "incident_id": "i1",
                "status_message": "blocked: no leader",
                "timestamp": "2026-07-13T00:00:00+00:00",
            },
            {"event": "incident-start", "unit": "b/0", "incident_id": "i2", "timestamp": "2026-07-14T00:00:00+00:00"},
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
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "increment": 3,
            "incident": inc.to_dict(),
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
        write_event(
            {"event": "incident-start", "unit": "a/0", "incident_id": "i1", "timestamp": "2026-10-04T20:09:03+00:00"},
            audit,
        )
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
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "unhealthy_since": "2026-01-01T00:00:00+00:00",
            "increment": 1,
            "incident": inc.to_dict(),
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
        write_event(
            {
                "event": "incident-start",
                "unit": "mysql/0",
                "incident_id": "old-id",
                "timestamp": "2026-10-04T20:09:03+00:00",
            },
            audit,
        )
        h = _make_harness({"audit-log-path": audit})
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        import json as _json

        archives = self._archives(tmp_path)
        assert len(archives) == 1
        closed = [
            _json.loads(line)
            for line in archives[0].read_text().splitlines()
            if _json.loads(line)["event"] == "incident-closed"
        ]
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
        write_event(
            {
                "event": "incident-start",
                "unit": "postgresql/0",
                "incident_id": inc.id,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
            audit,
        )
        h = _make_harness({"audit-log-path": audit})
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "increment": 3,
            "incident": inc.to_dict(),
        }
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_reset(event)
        import json as _json

        archives = self._archives(tmp_path)
        assert len(archives) == 1
        closed = [
            _json.loads(line)
            for line in archives[0].read_text().splitlines()
            if _json.loads(line)["event"] == "incident-closed"
        ]
        assert len(closed) == 1
        assert closed[0]["incident_id"] == inc.id


class TestAskAction:
    def _setup(self, tmp_path, config=None):
        cfg = {"mode": "suggest", "report-dir": str(tmp_path), "audit-log-path": str(tmp_path / "events.jsonl")}
        if config:
            cfg.update(config)
        h = _make_harness(cfg)
        inc = Incident.open("blocked")
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked",
            "increment": 1,
            "incident": inc.to_dict(),
        }
        with open(os.path.join(str(tmp_path), f"{inc.id}.md"), "w") as f:
            f.write("# Incident Report\n")
        return h, inc

    def _event(self, **params):
        ev = mock.MagicMock()
        params = {k.replace("_", "-"): v for k, v in params.items()}
        ev.params.get.side_effect = lambda k, d=None: params.get(k, d)
        return ev

    def test_ask_requires_question(self, tmp_path):
        h, inc = self._setup(tmp_path)
        ev = self._event()
        h.charm._on_action_ask(ev)
        assert "question is required" in ev.fail.call_args[0][0]

    def test_ask_requires_suggest_mode(self, tmp_path):
        h, inc = self._setup(tmp_path, {"mode": "observe"})
        ev = self._event(question="why?")
        h.charm._on_action_ask(ev)
        assert "requires mode=suggest" in ev.fail.call_args[0][0]

    def test_ask_appends_turn_and_never_overwrites(self, tmp_path):
        h, inc = self._setup(tmp_path)
        with mock.patch.object(
            h.charm, "_run_mode_logic", return_value=Suggestion.from_llm("because X", ["juju config a=b"])
        ):
            ev = self._event(incident_id=inc.id, question="why blocked?")
            h.charm._on_action_ask(ev)
        results = ev.set_results.call_args[0][0]
        assert results["incident-id"] == inc.id
        assert results["turn-id"] == 1
        assert "because X" in results["description"]

        with mock.patch.object(h.charm, "_run_mode_logic", return_value=Suggestion.from_llm("second answer", ["cmd2"])):
            ev2 = self._event(incident_id=inc.id, question="and then?")
            h.charm._on_action_ask(ev2)
        turns = h.charm._status_tracker._state["postgresql/0"]["incident"]["turns"]
        assert [t["description"] for t in turns] == ["because X", "second answer"]
        assert [t["id"] for t in turns] == [1, 2]
        assert ev2.set_results.call_args[0][0]["turn-id"] == 2
        assert turns[0]["question"] == "why blocked?"
        assert turns[1]["question"] == "and then?"

    def test_ask_redacts_secrets_in_the_question(self, tmp_path):
        h, inc = self._setup(tmp_path)
        with mock.patch.object(h.charm, "_run_mode_logic") as m:
            m.return_value = Suggestion.from_llm("ok", [])
            h.charm._on_action_ask(self._event(question="why? my password=hunter2 fails"))
        assert "hunter2" not in m.call_args.kwargs["question"]
        turns = h.charm._status_tracker._state["postgresql/0"]["incident"]["turns"]
        assert "hunter2" not in turns[0]["question"]
        assert "[REDACTED]" in turns[0]["question"]

    def test_ask_passes_question_and_history_to_mode_logic(self, tmp_path):
        h, inc = self._setup(tmp_path)
        h.charm._status_tracker._state["postgresql/0"]["incident"] = (
            Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"])
            .append_turn(Turn.from_suggestion(Suggestion.from_llm("prior answer", ["cmd0"])))
            .to_dict()
        )
        with mock.patch.object(h.charm, "_run_mode_logic") as m:
            m.return_value = Suggestion.from_llm("now", [])
            h.charm._on_action_ask(self._event(incident_id=inc.id, question="next?"))
            _, kwargs = m.call_args
            assert kwargs["question"] == "next?"
            assert kwargs["block_on_error"] is False
            assert kwargs["history"][0]["description"] == "prior answer"
            assert kwargs["history"][0]["commands"] == ["cmd0"]
            assert "additional_context" not in kwargs

    def test_ask_provider_error_fails_action_without_blocking_unit(self, tmp_path):
        from ops.model import ActiveStatus

        h, inc = self._setup(tmp_path, {"provider": "gemini", "api-token": "tok"})
        h.charm.unit.status = ActiveStatus("ok")
        provider = mock.MagicMock()
        provider.check.return_value = None
        provider.generate.side_effect = RuntimeError("timeout")
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(provider, None)):
            ev = self._event(incident_id=inc.id, question="why?")
            h.charm._on_action_ask(ev)
        assert "timeout" in ev.fail.call_args[0][0]
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "turns" not in h.charm._status_tracker._state["postgresql/0"]["incident"]

    def test_get_suggestion_blocks_unit_on_provider_error(self, tmp_path):
        from ops.model import BlockedStatus

        h, inc = self._setup(tmp_path, {"provider": "gemini", "api-token": "tok"})
        provider = mock.MagicMock()
        provider.check.return_value = None
        provider.generate.side_effect = RuntimeError("boom")
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(provider, None)):
            h.charm._on_action_get_suggestion(self._event())
        assert isinstance(h.charm.unit.status, BlockedStatus)

    def test_ask_without_id_needs_a_single_open_incident(self, tmp_path):
        h, inc = self._setup(tmp_path)
        other = Incident.open("blocked")
        h.charm._status_tracker._state["mysql/0"] = {
            "status": "blocked",
            "increment": 1,
            "incident": other.to_dict(),
        }
        ev = self._event(question="why?")
        h.charm._on_action_ask(ev)
        msg = ev.fail.call_args[0][0]
        assert "pass incident-id" in msg and inc.id in msg and other.id in msg

    def test_ask_without_id_ignores_closed_incident(self, tmp_path):
        h, inc = self._setup(tmp_path)
        h.charm._status_tracker._state["postgresql/0"]["incident"] = (
            Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"]).close().to_dict()
        )
        ev = self._event(question="why?")
        h.charm._on_action_ask(ev)
        assert "no open incident" in ev.fail.call_args[0][0]

    def test_ask_with_id_can_target_closed_incident(self, tmp_path):
        h, inc = self._setup(tmp_path)
        h.charm._status_tracker._state["postgresql/0"]["incident"] = (
            Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"]).close().to_dict()
        )
        with mock.patch.object(h.charm, "_run_mode_logic", return_value=Suggestion.from_llm("after recovery", [])):
            ev = self._event(incident_id=inc.id, question="what happened?")
            h.charm._on_action_ask(ev)
        assert not ev.fail.called, ev.fail.call_args
        assert ev.set_results.call_args[0][0]["turn-id"] == 1

    def _seed_cached_suggestion(self, h):
        incident = Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"])
        incident = incident.append_turn(Turn.from_suggestion(Suggestion.from_llm("stored", ["cmd"])))
        h.charm._status_tracker.update_incident("postgresql/0", incident.to_dict())

    def test_get_suggestion_serves_the_cache_by_default(self, tmp_path):
        h, inc = self._setup(tmp_path)
        self._seed_cached_suggestion(h)
        with mock.patch.object(h.charm, "_run_mode_logic") as m:
            ev = self._event()
            h.charm._on_action_get_suggestion(ev)
        m.assert_not_called()
        assert ev.set_results.call_args[0][0]["cached"] == "true"

    def test_get_suggestion_force_regenerates_despite_the_cache(self, tmp_path):
        h, inc = self._setup(tmp_path)
        self._seed_cached_suggestion(h)
        with mock.patch.object(h.charm, "_run_mode_logic", return_value=Suggestion.from_llm("fresh", ["new"])) as m:
            ev = self._event(force=True)
            h.charm._on_action_get_suggestion(ev)
        m.assert_called_once()
        results = ev.set_results.call_args[0][0]
        assert results["cached"] == "false"
        assert results["description"] == "fresh"
        turns = h.charm._status_tracker._state["postgresql/0"]["incident"]["turns"]
        assert [t["description"] for t in turns] == ["stored", "fresh"]

    def test_get_suggestion_force_still_needs_suggest_mode(self, tmp_path):
        h, inc = self._setup(tmp_path, {"mode": "observe"})
        self._seed_cached_suggestion(h)
        ev = self._event(force=True)
        h.charm._on_action_get_suggestion(ev)
        assert "mode is 'observe'" in ev.fail.call_args[0][0]

    def test_get_suggestion_cache_ignores_ask_turns(self, tmp_path):
        h, inc = self._setup(tmp_path)
        incident = (
            Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"])
            .append_turn(Turn.from_suggestion(Suggestion.from_llm("one-shot", [])))
            .append_turn(Turn.from_suggestion(Suggestion.from_llm("an answer", []), question="why?"))
        )
        h.charm._status_tracker.update_incident("postgresql/0", incident.to_dict())
        ev = self._event()
        h.charm._on_action_get_suggestion(ev)
        results = ev.set_results.call_args[0][0]
        assert results["description"] == "one-shot"
        assert results["turn-id"] == 1
        assert results["cached"] == "true"

    def test_get_suggestion_does_not_serve_ask_answer_as_cache(self, tmp_path):
        h, inc = self._setup(tmp_path)
        incident = Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"]).append_turn(
            Turn.from_suggestion(Suggestion.from_llm("an answer", []), question="why?")
        )
        h.charm._status_tracker.update_incident("postgresql/0", incident.to_dict())
        with mock.patch.object(h.charm, "_run_mode_logic", return_value=Suggestion.from_llm("fresh", [])):
            ev = self._event()
            h.charm._on_action_get_suggestion(ev)
        assert ev.set_results.call_args[0][0]["description"] == "fresh"
        assert ev.set_results.call_args[0][0]["cached"] == "false"

    def test_turns_are_capped_at_max_turns(self, tmp_path):
        from jaime.incident import MAX_TURNS

        h, inc = self._setup(tmp_path)
        for n in range(MAX_TURNS + 3):
            with mock.patch.object(h.charm, "_run_mode_logic", return_value=Suggestion.from_llm(f"a{n}", [])):
                h.charm._on_action_ask(self._event(incident_id=inc.id, question=f"q{n}"))
        turns = h.charm._status_tracker._state["postgresql/0"]["incident"]["turns"]
        assert len(turns) == MAX_TURNS
        assert turns[0]["id"] == 4 and turns[-1]["id"] == MAX_TURNS + 3
        ev = self._event(incident_id=inc.id)
        h.charm._on_action_list_suggestions(ev)
        assert len(json.loads(ev.set_results.call_args[0][0]["result"])) == MAX_TURNS

    def test_list_suggestions_returns_turns(self, tmp_path):
        h, inc = self._setup(tmp_path)
        inc_obj = Incident.from_dict(h.charm._status_tracker._state["postgresql/0"]["incident"])
        inc_obj = inc_obj.append_turn(Turn.from_suggestion(Suggestion.from_llm("a", []))).append_turn(
            Turn.from_suggestion(Suggestion.from_llm("b", []))
        )
        h.charm._status_tracker.update_incident("postgresql/0", inc_obj.to_dict())
        ev = self._event(incident_id=inc.id)
        h.charm._on_action_list_suggestions(ev)
        records = json.loads(ev.set_results.call_args[0][0]["result"])
        assert len(records) == 2
        assert records[0]["question"] == ""
        assert records[1]["description"] == "b"


class TestRunModeLogicProjection:
    """``_run_mode_logic`` bounds the prompt via ``max-prompt-bytes``, not the report."""

    _REPORT = (
        "# Incident Report\n"
        "- incident: abc\n"
        "- unit: foo/0\n"
        "- status: blocked\n\n"
        "## Executive summary\nUnit is in **blocked** state.\n\n"
        "## Disk usage\n```\n" + "\n".join(f"/dev/sda{i} {i}G used" for i in range(40)) + "\n```\n\n"
        "## Processes\n" + "\n".join(f"- process-{i}: running" for i in range(40)) + "\n"
    )

    def _harness_with_provider(self, config, provider):
        h = _make_harness(config)
        h._provider_patch = mock.patch.object(h.charm, "_get_ai_provider", return_value=(provider, None))
        h._provider_patch.start()
        provider.check.return_value = None
        return h

    def test_zero_budget_sends_the_report_unprojected(self):
        provider = mock.MagicMock()
        provider.generate.return_value = ("diagnosis\n```bash\ncmd\n```", None)
        h = self._harness_with_provider({"mode": "suggest", "provider": "gemini", "api-token": "t"}, provider)
        h.charm._run_mode_logic(self._REPORT)
        sent_prompt = provider.generate.call_args[0][0]
        assert self._REPORT in sent_prompt
        assert h.charm._last_projection is None

    def test_nonzero_budget_projects_the_prompt_not_the_report(self):
        provider = mock.MagicMock()
        provider.generate.return_value = ("diagnosis\n```bash\ncmd\n```", None)
        h = self._harness_with_provider(
            {"mode": "suggest", "provider": "gemini", "api-token": "t", "max-prompt-bytes": 50}, provider
        )
        h.charm._run_mode_logic(self._REPORT)
        sent_prompt = provider.generate.call_args[0][0]
        assert "digested for the prompt" in sent_prompt
        assert "## Executive summary" in sent_prompt

    def test_projection_metadata_is_recorded_for_the_audit_event(self):
        provider = mock.MagicMock()
        provider.generate.return_value = ("diagnosis", None)
        h = self._harness_with_provider(
            {"mode": "suggest", "provider": "gemini", "api-token": "t", "max-prompt-bytes": 50}, provider
        )
        h.charm._run_mode_logic(self._REPORT)
        assert h.charm._last_projection["version"] >= 1
        assert h.charm._last_projection["budget_bytes"] == 50
        assert h.charm._last_projection["report_bytes"] == len(self._REPORT.encode())
        assert h.charm._last_projection["prompt_bytes"] < h.charm._last_projection["report_bytes"]

    def test_ask_is_projected_too(self):
        provider = mock.MagicMock()
        provider.generate.return_value = ("an answer", None)
        h = self._harness_with_provider(
            {"mode": "suggest", "provider": "gemini", "api-token": "t", "max-prompt-bytes": 50}, provider
        )
        h.charm._run_mode_logic(self._REPORT, question="what is wrong?")
        sent_prompt = provider.generate.call_args[0][0]
        assert "digested for the prompt" in sent_prompt
        assert "what is wrong?" in sent_prompt


class TestProviderCheckCache:
    def _charm(self, tmp_path):
        h = _make_harness({"mode": "suggest", "provider": "remote", "provider-remote-url": "http://x/v1", "model": "m"})
        h.charm._status_tracker._path = str(tmp_path / "status-state.json")
        return h.charm

    def test_success_is_cached_between_calls(self, tmp_path):
        charm = self._charm(tmp_path)
        provider = mock.MagicMock()
        provider.check.return_value = None
        assert charm._check_provider(provider) is None
        assert charm._check_provider(provider) is None
        assert provider.check.call_count == 1

    def test_failure_is_never_cached(self, tmp_path):
        charm = self._charm(tmp_path)
        provider = mock.MagicMock()
        provider.check.return_value = "HTTP 401"
        assert charm._check_provider(provider) == "HTTP 401"
        assert charm._check_provider(provider) == "HTTP 401"
        assert provider.check.call_count == 2

    def test_changed_settings_invalidate_the_cache(self, tmp_path):
        h = _make_harness({"mode": "suggest", "provider": "remote", "provider-remote-url": "http://x/v1", "model": "m"})
        h.charm._status_tracker._path = str(tmp_path / "status-state.json")
        provider = mock.MagicMock()
        provider.check.return_value = None
        h.charm._check_provider(provider)
        h.update_config({"model": "other"})
        h.charm._check_provider(provider)
        assert provider.check.call_count == 2

    def test_expired_entry_is_checked_again(self, tmp_path):
        import datetime

        from jaime.check_cache import CHECK_TTL_SECONDS, CheckCache

        cache = CheckCache(str(tmp_path / "c"))
        t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        cache.record("k", t0)
        assert cache.is_fresh("k", t0 + datetime.timedelta(seconds=CHECK_TTL_SECONDS - 1))
        assert not cache.is_fresh("k", t0 + datetime.timedelta(seconds=CHECK_TTL_SECONDS + 1))
        assert not cache.is_fresh("other", t0)

    def test_unwritable_cache_does_not_break_the_check(self):
        import datetime

        from jaime.check_cache import CheckCache

        cache = CheckCache("/proc/nope/cache")
        now = datetime.datetime.now(datetime.timezone.utc)
        cache.record("k", now)
        assert not cache.is_fresh("k", now)


class TestControllerSession:
    def test_missing_credentials_raise(self):
        from jaime.controller import ControllerError

        h = _make_harness()
        with pytest.raises(ControllerError, match="must be configured"):
            with h.charm._controller_session():
                pass

    def test_query_returns_default_when_unconfigured(self):
        h = _make_harness()
        assert h.charm._controller_query(lambda client: {"x": 1}, {}, "thing") == {}

    def test_query_returns_result_through_a_logged_in_client(self):
        h = _make_harness()
        client = mock.MagicMock()
        client.full_status.return_value = {"applications": {}}
        cm = mock.MagicMock()
        cm.__enter__.return_value = client
        with (
            mock.patch.object(h.charm, "_has_controller_credentials", return_value=True),
            mock.patch.object(h.charm, "_resolve_juju_password", return_value="pw"),
            mock.patch("jaime.core.agent_conf_path", return_value="/tmp/agent.conf"),
            mock.patch(
                "jaime.core.parse_agent_conf",
                return_value={"api_address": "a", "ca_cert": "c", "model_uuid": "m"},
            ),
            mock.patch("jaime.core.JujuControllerClient", return_value=cm),
        ):
            result = h.charm._controller_query(lambda c: c.full_status(), {}, "status")
        assert result == {"applications": {}}
        client.login.assert_called_once()

    def test_query_returns_default_when_the_query_fails(self):
        h = _make_harness()
        cm = mock.MagicMock()
        cm.__enter__.return_value.full_status.side_effect = RuntimeError("down")
        with (
            mock.patch.object(h.charm, "_has_controller_credentials", return_value=True),
            mock.patch("jaime.core.agent_conf_path", return_value="/tmp/agent.conf"),
            mock.patch(
                "jaime.core.parse_agent_conf",
                return_value={"api_address": "a", "ca_cert": "c", "model_uuid": "m"},
            ),
            mock.patch("jaime.core.JujuControllerClient", return_value=cm),
        ):
            assert h.charm._controller_query(lambda c: c.full_status(), {"d": 1}, "status") == {"d": 1}
