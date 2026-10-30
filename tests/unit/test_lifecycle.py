"""Incident lifecycle tests for the code review follow-ups (TASKS 4.16).

Covers: Juju timestamp parsing, one incident per episode with cooldown
refreshes, recording an incident only once its report exists, status
aggregation across units, report versions, the per-hook FullStatus cache and
the stored AI error.
"""

import datetime
import json
import os
import unittest.mock as mock

import pytest
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from jaime.controller import ControllerError
from jaime.core import CoreMixin
from jaime.incident import Suggestion, UsageMetadata
from jaime.logging import list_incidents
from jaime.principal import StatusTracker

_CONFIG_YAML = """
options:
  mode: {type: string, default: observe}
  provider: {type: string, default: none}
  model: {type: string, default: ""}
  api-token: {type: string, default: ""}
  provider-remote-url: {type: string, default: ""}
  watch-statuses: {type: string, default: "error,blocked"}
  failure-timeout-minutes: {type: int, default: 5}
  cooldown-minutes: {type: int, default: 30}
  report-dir: {type: string, default: ""}
  audit-log-path: {type: string, default: ""}
  juju-api-user: {type: string, default: ""}
  juju-api-password: {type: string, default: ""}
"""

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


class _Charm(CoreMixin, CharmBase):
    collect_error: Exception | None = None

    def __init__(self, *args):
        super().__init__(*args)
        self.collected: list[tuple[str, datetime.datetime | None]] = []

    def _collect_context(self, unit_name, from_time):
        if self.collect_error is not None:
            raise self.collect_error
        self.collected.append((unit_name, from_time))
        return {"unit_logs": [f"collected for {unit_name}"]}


@pytest.fixture
def harness(tmp_path):
    h = Harness(_Charm, config=_CONFIG_YAML)
    h.update_config({"report-dir": str(tmp_path / "reports"), "audit-log-path": str(tmp_path / "events.jsonl")})
    h.begin()
    h.charm._status_tracker = StatusTracker(str(tmp_path / "state.json"))
    _Charm.collect_error = None
    yield h
    _Charm.collect_error = None


def _at(minutes: float):
    return mock.patch("jaime.core.utc_now", return_value=T0 + datetime.timedelta(minutes=minutes))


def _events(tmp_path, kind=None):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in events if kind is None or e["event"] == kind]


def _process(h, minutes, unit="pg/0", status="blocked", since=None, message="boom"):
    with _at(minutes):
        return h.charm._process_unit(unit, status, since or T0.isoformat(), message)


class TestJujuTimestamps:
    def test_failure_timeout_honoured_for_rfc3339_nano_since(self, harness):
        # Juju FullStatus style: Z suffix and nanoseconds. Python 3.10's
        # fromisoformat rejects it, which used to open an incident at once.
        since = "2026-01-01T00:00:00.123456789Z"
        status = _process(harness, 1, since=since)
        assert "waiting" in status.message
        assert harness.charm._status_tracker.current_incident("pg/0") is None


class TestOneIncidentPerEpisode:
    def test_cooldown_refreshes_the_same_incident(self, harness, tmp_path):
        _process(harness, 6)
        incident = harness.charm._status_tracker.current_incident("pg/0")
        with mock.patch.object(_Charm, "_run_mode_logic") as run:
            _process(harness, 20)  # within cooldown: nothing written
            _process(harness, 40)  # cooldown elapsed: refresh
            _process(harness, 80)  # elapsed again: refresh
        run.assert_not_called()

        assert harness.charm._status_tracker.current_incident("pg/0")["id"] == incident["id"]
        assert len(_events(tmp_path, "incident-start")) == 1
        reasons = [e["reason"] for e in _events(tmp_path, "report-generated")]
        assert reasons == ["open", "refresh", "refresh"]
        reports = sorted(os.listdir(tmp_path / "reports"))
        assert len(reports) == 3
        assert f"{incident['id']}.md" in reports

    def test_recovery_after_long_episode_leaves_nothing_open(self, harness, tmp_path):
        for minutes in (6, 40, 80):
            _process(harness, minutes)
        _process(harness, 90, status="active")
        records = list_incidents(str(tmp_path / "events.jsonl"))
        assert [r["status"] for r in records] == ["closed"]

    def test_refresh_becomes_the_latest_report(self, harness):
        _process(harness, 6)
        first = harness.charm._status_tracker.current_incident("pg/0")
        _process(harness, 40)
        latest = harness.charm._status_tracker.current_incident("pg/0")
        assert latest["report_file"] != first["report_file"]
        assert latest["report_file"].startswith(first["id"] + "-")


class TestIncidentRecordedAfterReport:
    def test_collection_failure_opens_nothing_and_is_retried(self, harness, tmp_path):
        _Charm.collect_error = ValueError("No closing quotation")
        status = _process(harness, 6)
        assert isinstance(status, BlockedStatus)
        assert "report failed for pg/0" in status.message
        assert harness.charm._status_tracker.current_incident("pg/0") is None
        failed = _events(tmp_path, "incident-report-failed")
        assert len(failed) == 1
        assert "incident_id" not in failed[0]
        assert list_incidents(str(tmp_path / "events.jsonl")) == []

        _Charm.collect_error = None
        status = _process(harness, 11)
        assert status.message.startswith("incident open")
        incident = harness.charm._status_tracker.current_incident("pg/0")
        assert os.path.exists(tmp_path / "reports" / f"{incident['id']}.md")

    def test_failed_refresh_keeps_incident_and_waits_a_cooldown(self, harness, tmp_path):
        _process(harness, 6)
        incident = harness.charm._status_tracker.current_incident("pg/0")
        _Charm.collect_error = OSError("disk full")
        status = _process(harness, 40)
        assert status.message.startswith("incident open")
        assert harness.charm._status_tracker.current_incident("pg/0") == incident
        assert len(_events(tmp_path, "incident-report-failed")) == 1
        _process(harness, 50)  # within the new cooldown: not retried yet
        assert len(_events(tmp_path, "incident-report-failed")) == 1


class TestStatusAggregation:
    def test_healthy_unit_processed_last_does_not_hide_an_incident(self, harness):
        with _at(10):
            harness.charm._process_units(
                [
                    ("pg/0", "blocked", T0.isoformat(), "boom"),
                    ("pg/1", "active", T0.isoformat(), ""),
                ]
            )
        assert isinstance(harness.charm.unit.status, ActiveStatus)
        assert harness.charm.unit.status.message.startswith("incident open: pg/0")

    def test_blocked_wins_over_waiting_and_ready(self, harness):
        _Charm.collect_error = RuntimeError("nope")  # pg/0's report fails: blocked
        with _at(10):
            harness.charm._process_units(
                [
                    ("pg/0", "blocked", T0.isoformat(), ""),
                    ("pg/1", "blocked", (T0 + datetime.timedelta(minutes=9)).isoformat(), ""),
                    ("pg/2", "active", T0.isoformat(), ""),
                ]
            )
        assert isinstance(harness.charm.unit.status, BlockedStatus)


class TestStoredAiError:
    def test_ai_failure_keeps_unit_blocked_through_cooldown(self, harness):
        harness.update_config({"mode": "suggest", "provider": "gemini", "api-token": "tok"})
        provider = mock.MagicMock()
        provider.check.return_value = "HTTP 401"
        with mock.patch.object(_Charm, "_get_ai_provider", return_value=(provider, None)):
            status = _process(harness, 6)
        assert isinstance(status, BlockedStatus)
        assert "AI provider error" in status.message

        status = _process(harness, 10)  # cooldown: still blocked on the stored error
        assert isinstance(status, BlockedStatus)

    def test_successful_turn_clears_the_error(self, harness):
        _process(harness, 6)
        harness.charm._status_tracker.set_ai_error("pg/0", "AI provider error: boom")
        incident = harness.charm._status_tracker.current_incident("pg/0")
        harness.charm._store_suggestion("pg/0", incident, Suggestion.from_llm("ok", []))
        assert _process(harness, 10).message.startswith("incident open")


class TestReportVersions:
    def _open(self, harness):
        _process(harness, 6)
        return harness.charm._status_tracker.current_incident("pg/0")

    def test_generate_report_never_overwrites(self, harness, tmp_path):
        incident = self._open(harness)
        original = tmp_path / "reports" / f"{incident['id']}.md"
        before = original.read_text()

        event = mock.MagicMock()
        harness.charm._on_action_generate_report(event)
        path = event.set_results.call_args[0][0]["report-path"]

        assert os.path.basename(path) != original.name
        assert original.read_text() == before
        assert harness.charm._status_tracker.current_incident("pg/0")["report_file"] == os.path.basename(path)
        assert _events(tmp_path, "report-generated")[-1]["reason"] == "manual"

    def test_generate_report_uses_the_incident_window(self, harness):
        self._open(harness)
        harness.charm._on_action_generate_report(mock.MagicMock())
        assert harness.charm.collected[-1] == ("pg/0", T0)

    def test_generate_report_failure_fails_the_action(self, harness):
        self._open(harness)
        _Charm.collect_error = RuntimeError("k8s token missing")
        event = mock.MagicMock()
        harness.charm._on_action_generate_report(event)
        event.fail.assert_called_once()

    def test_ask_reads_latest_version_and_turn_records_it(self, harness, tmp_path):
        incident = self._open(harness)
        harness.charm._on_action_generate_report(mock.MagicMock())
        latest = harness.charm._status_tracker.current_incident("pg/0")["report_file"]
        (tmp_path / "reports" / latest).write_text("LATEST EVIDENCE")

        harness.update_config({"mode": "suggest"})
        event = mock.MagicMock()
        event.params = {"question": "why?", "incident-id": incident["id"]}
        answer = Suggestion.from_llm("because", [], usage=UsageMetadata(model="m"))
        with mock.patch.object(_Charm, "_run_mode_logic", return_value=answer) as run:
            harness.charm._on_action_ask(event)
        assert run.call_args[0][0] == "LATEST EVIDENCE"
        turn = harness.charm._status_tracker.current_incident("pg/0")["turns"][-1]
        assert turn["report_file"] == latest

    def test_cached_suggestion_is_stale_after_a_refresh(self, harness):
        incident = self._open(harness)
        harness.charm._store_suggestion("pg/0", incident, Suggestion.from_llm("old", []))
        stored = harness.charm._status_tracker.current_incident("pg/0")
        assert harness.charm._cached_suggestion(stored, "", "") is not None

        harness.charm._on_action_generate_report(mock.MagicMock())
        refreshed = harness.charm._status_tracker.current_incident("pg/0")
        assert harness.charm._cached_suggestion(refreshed, "", "") is None


class TestReset:
    def test_closed_incident_gets_no_second_closure(self, harness, tmp_path):
        _process(harness, 6)
        _process(harness, 7, status="active")
        _process(harness, 8, unit="pg/1")
        _process(harness, 14, unit="pg/1")

        harness.charm._on_action_reset(mock.MagicMock())

        archive = next(p for p in tmp_path.iterdir() if p.name.startswith("events.jsonl.2"))
        closures: dict[str, list[str]] = {}
        for line in archive.read_text().splitlines():
            event = json.loads(line)
            if event["event"] == "incident-closed":
                closures.setdefault(event["unit"], []).append(event.get("reason", "recovery"))
        assert closures == {"pg/0": ["recovery"], "pg/1": ["manual reset"]}


class TestInvalidMode:
    @pytest.mark.parametrize("action", ["_on_action_ask", "_on_action_get_suggestion"])
    def test_actions_fail_cleanly(self, harness, action):
        _process(harness, 6)
        incident = harness.charm._status_tracker.current_incident("pg/0")
        harness.charm._store_suggestion("pg/0", incident, Suggestion.from_llm("x", []))
        harness.charm._on_action_generate_report(mock.MagicMock())  # make the cache stale
        harness.update_config({"mode": "diagnose"})
        event = mock.MagicMock()
        event.params = {"question": "q", "incident-id": "", "additional-context": "", "force": False}
        getattr(harness.charm, action)(event)
        event.fail.assert_called_once()
        assert "mode" in event.fail.call_args[0][0]


class TestFullStatusCache:
    def _session(self, client):
        cm = mock.MagicMock()
        cm.__enter__.return_value = client
        cm.__exit__.return_value = False
        return cm

    def test_one_fetch_per_hook(self, harness):
        client = mock.MagicMock()
        client.full_status.return_value = {"applications": {}}
        with mock.patch.object(_Charm, "_controller_session", return_value=self._session(client)) as session:
            with harness.charm._hook_cache():
                harness.charm._full_status()
                harness.charm._full_status()
        assert session.call_count == 1
        assert client.full_status.call_count == 1

    def test_failure_cached_for_the_hook(self, harness):
        with mock.patch.object(_Charm, "_controller_session", side_effect=ControllerError("down")) as session:
            with harness.charm._hook_cache():
                for _ in range(2):
                    with pytest.raises(ControllerError):
                        harness.charm._full_status()
        assert session.call_count == 1

    def test_unexpected_errors_become_controller_errors(self, harness):
        client = mock.MagicMock()
        client.full_status.side_effect = TimeoutError("recv timed out")
        with mock.patch.object(_Charm, "_controller_session", return_value=self._session(client)):
            with harness.charm._hook_cache(), pytest.raises(ControllerError):
                harness.charm._full_status()

    def test_cache_does_not_outlive_the_handler(self, harness):
        client = mock.MagicMock()
        client.full_status.return_value = {"applications": {}}
        with mock.patch.object(_Charm, "_controller_session", return_value=self._session(client)) as session:
            with harness.charm._hook_cache():
                harness.charm._full_status()
            with harness.charm._hook_cache():
                harness.charm._full_status()
        assert session.call_count == 2
