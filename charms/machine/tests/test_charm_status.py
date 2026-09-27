"""Unit tests for _log_principal_status in JaimeCharm."""

import datetime
import json
import logging
import unittest.mock as mock

from charm import JaimeCharm
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness

from jaime.principal import StatusTracker


def _try_json(s, default=None):
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}

# Fixed timestamps used across tests
SINCE = datetime.datetime(2026, 7, 13, 10, 0, 0, tzinfo=datetime.timezone.utc)
SINCE_ISO = SINCE.isoformat()

_DEFAULT_CONFIG = {
    "watch-statuses": "error,blocked",
    "failure-timeout-minutes": 5,
    "cooldown-minutes": 30,
}


def make_goal_relations(status, since=SINCE):
    """Return a goal-state relations dict for the principal endpoint."""
    unit_goal = mock.MagicMock()
    unit_goal.status = status
    unit_goal.since = since
    app_goal = mock.MagicMock()
    app_goal.status = "joined"
    app_goal.since = since
    return {"postgresql/0": unit_goal, "postgresql": app_goal}


def _is_incident_opened(record):
    try:
        return json.loads(record.getMessage()).get("event") == "incident-opened"
    except (json.JSONDecodeError, AttributeError):
        return False


def _is_cooldown(record):
    try:
        return json.loads(record.getMessage()).get("event") == "principal-status-cooldown"
    except (json.JSONDecodeError, AttributeError):
        return False


def make_harness(tmp_path, config_overrides=None):
    """Return a started Harness with diagnostics side-effects suppressed."""
    cfg = {**_DEFAULT_CONFIG, **(config_overrides or {})}
    with mock.patch.object(JaimeCharm, "_ensure_diagnostics"):
        h = Harness(JaimeCharm)
        h.update_config(cfg)
        h.begin()
    h.charm._status_tracker = StatusTracker(state_path=str(tmp_path / "state.json"))
    return h


def call_log_status(harness, goal_relations, now):
    """Call _log_principal_status with mocked goal_state and datetime."""
    gs = mock.MagicMock()
    gs.relations = {"principal": goal_relations}

    log_records = []

    class CapturingHandler(logging.Handler):
        def emit(self, record):
            log_records.append(record)

    handler = CapturingHandler()
    charm_logger = logging.getLogger("charm")
    core_logger = logging.getLogger("jaime.core")
    charm_logger.addHandler(handler)
    core_logger.addHandler(handler)
    charm_logger.setLevel(logging.DEBUG)
    core_logger.setLevel(logging.DEBUG)

    try:
        with mock.patch("charm.goal_state", return_value=gs), \
             mock.patch("charm.datetime") as mock_dt, \
             mock.patch("jaime.core.datetime") as core_dt, \
             mock.patch("charm.collect_context", return_value={}), \
             mock.patch("jaime.core.generate_report", return_value="/tmp/test-report.md"), \
             mock.patch("charm.write_event"), \
             mock.patch("jaime.core.write_event"), \
             mock.patch("builtins.open", mock.mock_open(read_data="report content")):
            mock_dt.datetime.now.return_value = now
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timezone.utc = datetime.timezone.utc
            core_dt.datetime.now.return_value = now
            core_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            core_dt.timezone.utc = datetime.timezone.utc
            harness.charm._log_principal_status()
    finally:
        charm_logger.removeHandler(handler)
        core_logger.removeHandler(handler)

    return log_records


class TestWatchedStatusLogged:
    def test_blocked_emits_json_event(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        records = call_log_status(h, make_goal_relations("blocked"), now)

        watched = [r for r in records if "principal-status-watched" in r.getMessage()]
        assert len(watched) == 1
        entry = json.loads(watched[0].getMessage())
        assert entry["event"] == "principal-status-watched"
        assert entry["unit"] == "postgresql/0"
        assert entry["workload"] == "blocked"
        assert entry["first_seen"] == SINCE_ISO
        assert entry["increment"] == 1

    def test_active_not_in_watched_emits_no_json_event(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        records = call_log_status(h, make_goal_relations("active"), now)
        assert not any("principal-status-watched" in r.getMessage() for r in records)

    def test_custom_watch_statuses_respected(self, tmp_path):
        h = make_harness(tmp_path, config_overrides={"watch-statuses": "error"})
        now = SINCE + datetime.timedelta(minutes=10)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        assert not any("principal-status-watched" in r.getMessage() for r in records)

    def test_increment_increases_on_consecutive_ticks(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), now)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        watched = [r for r in records if "principal-status-watched" in r.getMessage()]
        assert json.loads(watched[0].getMessage())["increment"] == 2


class TestFailureTimeout:
    def test_no_incident_before_timeout(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=2)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        assert not any(_is_incident_opened(r) for r in records)
        assert any("waiting for failure-timeout" in r.getMessage() for r in records)

    def test_incident_after_timeout(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=6)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        assert any(_is_incident_opened(r) for r in records)

    def test_incident_at_exactly_timeout(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=5)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        assert any(_is_incident_opened(r) for r in records)

    def test_incident_contains_first_seen(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=6)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        incident_record = next(r for r in records if _is_incident_opened(r))
        entry = json.loads(incident_record.getMessage())
        assert entry["first_seen"] == SINCE_ISO

    def test_incident_has_uuid_and_opened_at(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=6)
        records = call_log_status(h, make_goal_relations("blocked"), now)
        incident_record = next(r for r in records if _is_incident_opened(r))
        entry = json.loads(incident_record.getMessage())
        assert "incident" in entry
        assert "id" in entry["incident"]
        assert "opened_at" in entry["incident"]
        # UUID format check
        import uuid
        uuid.UUID(entry["incident"]["id"])  # raises if invalid


class TestFlappingWorkload:
    """A charm looping on a failure re-sets its status, bumping Juju's `since`.

    The unhealthy timer must be anchored to when Jaime first saw the unit go
    unhealthy, otherwise such a unit never reaches failure-timeout-minutes and
    no incident is ever opened.
    """

    def test_since_bump_does_not_reset_timer(self, tmp_path):
        h = make_harness(tmp_path, config_overrides={"failure-timeout-minutes": 1})

        # t=0: first seen unhealthy.
        opened = []
        call_log_status(h, make_goal_relations("blocked", since=SINCE), SINCE)

        # The charm re-sets its status every 30s, so `since` keeps moving up.
        # Juju would report only 0.0 min unhealthy each time.
        for elapsed in (30, 60, 90):
            tick = SINCE + datetime.timedelta(seconds=elapsed)
            records = call_log_status(
                h, make_goal_relations("blocked", since=tick), tick
            )
            opened += [r for r in records if _is_incident_opened(r)]

        # Continuous unhealthiness crosses the 1 min timeout exactly once.
        assert len(opened) == 1

    def test_flapping_between_watched_statuses_opens_one_incident(self, tmp_path):
        h = make_harness(
            tmp_path,
            config_overrides={
                "watch-statuses": "error,blocked,maintenance",
                "failure-timeout-minutes": 1,
            },
        )

        opened = []
        for elapsed, status in (
            (0, "maintenance"),
            (25, "blocked"),
            (50, "maintenance"),
            (75, "blocked"),
            (100, "maintenance"),
        ):
            tick = SINCE + datetime.timedelta(seconds=elapsed)
            records = call_log_status(
                h, make_goal_relations(status, since=tick), tick
            )
            opened += [r for r in records if _is_incident_opened(r)]

        assert len(opened) == 1, "flapping must not open a second incident"

    def test_timer_anchored_at_first_unhealthy_observation(self, tmp_path):
        h = make_harness(
            tmp_path,
            config_overrides={
                "watch-statuses": "error,blocked,maintenance",
                "failure-timeout-minutes": 1,
            },
        )
        call_log_status(h, make_goal_relations("maintenance", since=SINCE), SINCE)

        later = SINCE + datetime.timedelta(seconds=90)
        records = call_log_status(
            h, make_goal_relations("blocked", since=later), later
        )
        incident_record = next(r for r in records if _is_incident_opened(r))
        entry = json.loads(incident_record.getMessage())
        assert entry["first_seen"] == SINCE_ISO
        assert entry["status_since"] == later.isoformat()

    def test_recovery_re_arms_the_timer(self, tmp_path):
        h = make_harness(tmp_path, config_overrides={"failure-timeout-minutes": 1})
        call_log_status(h, make_goal_relations("blocked", since=SINCE), SINCE)

        recovered_at = SINCE + datetime.timedelta(seconds=30)
        call_log_status(
            h, make_goal_relations("active", since=recovered_at), recovered_at
        )

        # Unhealthy again: the timer restarts from here, so no immediate incident.
        broke_at = SINCE + datetime.timedelta(seconds=40)
        records = call_log_status(
            h, make_goal_relations("blocked", since=broke_at), broke_at
        )
        assert not any(_is_incident_opened(r) for r in records)


class TestCooldown:
    def test_second_incident_suppressed_within_cooldown(self, tmp_path):
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        records1 = call_log_status(h, make_goal_relations("blocked"), first_now)
        assert any(_is_incident_opened(r) for r in records1)

        second_now = SINCE + datetime.timedelta(minutes=20)
        records2 = call_log_status(h, make_goal_relations("blocked"), second_now)
        assert not any(_is_incident_opened(r) for r in records2)
        assert any(_is_cooldown(r) for r in records2)

    def test_cooldown_survives_a_since_bump(self, tmp_path):
        """A `since` bump must not clear the cooldown and re-report."""
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        records1 = call_log_status(h, make_goal_relations("blocked"), first_now)
        assert any(_is_incident_opened(r) for r in records1)

        bumped_since = SINCE + datetime.timedelta(minutes=15)
        second_now = SINCE + datetime.timedelta(minutes=20)
        records2 = call_log_status(
            h, make_goal_relations("blocked", since=bumped_since), second_now
        )
        assert not any(_is_incident_opened(r) for r in records2)
        assert any(_is_cooldown(r) for r in records2)

    def test_cooldown_log_includes_incident(self, tmp_path):
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), first_now)

        second_now = SINCE + datetime.timedelta(minutes=20)
        records2 = call_log_status(h, make_goal_relations("blocked"), second_now)
        cooldown_record = next(r for r in records2 if _is_cooldown(r))
        entry = json.loads(cooldown_record.getMessage())
        assert entry["event"] == "principal-status-cooldown"
        assert "incident" in entry
        assert entry["incident"] is not None
        assert "id" in entry["incident"]
        assert "opened_at" in entry["incident"]
        assert "cooldown_elapsed_minutes" in entry
        assert "cooldown_minutes" in entry

    def test_second_incident_fires_after_cooldown(self, tmp_path):
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), first_now)

        second_now = SINCE + datetime.timedelta(minutes=45)
        records = call_log_status(h, make_goal_relations("blocked"), second_now)
        assert any(_is_incident_opened(r) for r in records)


class TestRecovery:
    def test_recovery_emits_recovered_event(self, tmp_path):
        h = make_harness(tmp_path)
        h.charm._status_tracker.observe("postgresql/0", "blocked", SINCE_ISO)

        recovery_since = SINCE + datetime.timedelta(minutes=30)
        now = SINCE + datetime.timedelta(minutes=35)
        records = call_log_status(h, make_goal_relations("active", since=recovery_since), now)

        recovered = [r for r in records if "principal-status-recovered" in r.getMessage()]
        assert len(recovered) == 1
        entry = json.loads(recovered[0].getMessage())
        assert entry["event"] == "principal-status-recovered"
        assert entry["unit"] == "postgresql/0"
        assert entry["workload"] == "active"

    def test_new_episode_same_status_clears_cooldown(self, tmp_path):
        """Same status string but new since → new episode → cooldown cleared."""
        h = make_harness(tmp_path)
        since_1 = SINCE
        since_2 = SINCE + datetime.timedelta(hours=1)

        h.charm._status_tracker.observe("postgresql/0", "blocked", since_1.isoformat())
        h.charm._status_tracker.record_reported(
            "postgresql/0",
            (since_1 + datetime.timedelta(minutes=10)).isoformat(),
            {"id": "test-uuid", "opened_at": since_1.isoformat()},
        )

        now_2 = since_2 + datetime.timedelta(minutes=10)
        records = call_log_status(h, make_goal_relations("blocked", since=since_2), now_2)
        assert any(_is_incident_opened(r) for r in records)
        assert not any("cooldown active" in r.getMessage() for r in records)

    def test_recovery_resets_increment_to_one(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), now)
        call_log_status(h, make_goal_relations("blocked"), now)

        recovery_since = SINCE + datetime.timedelta(minutes=20)
        call_log_status(h, make_goal_relations("active", since=recovery_since), now)

        assert h.charm._status_tracker._state["postgresql/0"]["increment"] == 1

    def test_recovery_closes_open_incident(self, tmp_path):
        h = make_harness(tmp_path)
        # Open an incident
        first_now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), first_now)

        # Principal recovers
        recovery_since = SINCE + datetime.timedelta(minutes=20)
        now = SINCE + datetime.timedelta(minutes=25)
        call_log_status(h, make_goal_relations("active", since=recovery_since), now)

        incident = h.charm._status_tracker.current_incident("postgresql/0")
        assert incident is not None
        assert incident.get("closed_at") is not None

    def test_recovery_logs_incident_closed_event(self, tmp_path):
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), first_now)

        recovery_since = SINCE + datetime.timedelta(minutes=20)
        now = SINCE + datetime.timedelta(minutes=25)
        records = call_log_status(h, make_goal_relations("active", since=recovery_since), now)

        closed_events = [
            r for r in records
            if _try_json(r.getMessage(), {}).get("event") == "incident-closed"
        ]
        assert len(closed_events) == 1
        entry = json.loads(closed_events[0].getMessage())
        assert "incident" in entry
        assert entry["incident"]["closed_at"] is not None


class TestUnitStatus:
    def test_waiting_status_set_within_failure_timeout(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=2)
        call_log_status(h, make_goal_relations("blocked"), now)
        assert isinstance(h.charm.unit.status, WaitingStatus)
        assert "waiting" in h.charm.unit.status.message

    def test_maintenance_status_set_when_incident_opened(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), now)
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "incident open" in h.charm.unit.status.message

    def test_maintenance_status_set_during_cooldown(self, tmp_path):
        h = make_harness(tmp_path)
        first_now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), first_now)
        second_now = SINCE + datetime.timedelta(minutes=20)
        call_log_status(h, make_goal_relations("blocked"), second_now)
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "incident open" in h.charm.unit.status.message

    def test_active_status_set_on_recovery(self, tmp_path):
        h = make_harness(tmp_path)
        now = SINCE + datetime.timedelta(minutes=10)
        call_log_status(h, make_goal_relations("blocked"), now)

        recovery_since = SINCE + datetime.timedelta(minutes=20)
        call_log_status(h, make_goal_relations("active", since=recovery_since), now)
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"


class TestGoalStateError:
    def test_goal_state_exception_logs_warning(self, tmp_path):
        h = make_harness(tmp_path)
        log_records = []

        class Cap(logging.Handler):
            def emit(self, r):
                log_records.append(r)

        handler = Cap()
        charm_logger = logging.getLogger("charm")
        charm_logger.addHandler(handler)
        charm_logger.setLevel(logging.WARNING)
        try:
            with mock.patch("ops.hookcmds.goal_state", side_effect=RuntimeError("socket error")):
                h.charm._log_principal_status()
        finally:
            charm_logger.removeHandler(handler)

        assert any("could not read principal goal-state" in r.getMessage() for r in log_records)


class TestShowUsageAction:
    """Tests for _on_action_show_usage."""

    def _make_harness(self, tmp_path):
        cfg = {**_DEFAULT_CONFIG}
        with mock.patch.object(JaimeCharm, "_ensure_diagnostics"):
            h = Harness(JaimeCharm)
            h.update_config(cfg)
            h.begin()
        h.charm._status_tracker = StatusTracker(state_path=str(tmp_path / "state.json"))
        return h

    def _seed_usage(self, h, unit="postgresql/0", incident_id="abc-123"):
        """Seed a usage log entry directly into the tracker."""
        h.charm._status_tracker._state[unit] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "increment": 1, "usage_log": [],
        }
        h.charm._status_tracker.record_usage(unit, {
            "incident_id": incident_id,
            "timestamp": "2026-01-01T00:01:00+00:00",
            "model": "deepseek/deepseek-chat",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.001,
        })

    def test_no_usage_returns_message(self, tmp_path):
        h = self._make_harness(tmp_path)
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": ""}
        h.charm._on_action_show_usage(action_event)
        call_args = action_event.set_results.call_args[0][0]
        result = json.loads(call_args["result"])
        assert "message" in result

    def test_global_summary(self, tmp_path):
        h = self._make_harness(tmp_path)
        self._seed_usage(h)
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": ""}
        h.charm._on_action_show_usage(action_event)
        call_args = action_event.set_results.call_args[0][0]
        result = json.loads(call_args["result"])
        assert result["total_tokens"] == 150
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["cost_usd"] == 0.001
        assert result["total_incidents"] == 1
        assert result["total_calls"] == 1
        assert "deepseek/deepseek-chat" in result["by_model"]
        assert result["by_model"]["deepseek/deepseek-chat"]["calls"] == 1

    def test_per_incident_filter(self, tmp_path):
        h = self._make_harness(tmp_path)
        self._seed_usage(h, unit="postgresql/0", incident_id="abc-123")
        self._seed_usage(h, unit="postgresql/1", incident_id="xyz-999")
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": "abc-123"}
        h.charm._on_action_show_usage(action_event)
        call_args = action_event.set_results.call_args[0][0]
        result = json.loads(call_args["result"])
        assert result["incident_id"] == "abc-123"
        assert result["total_tokens"] == 150

    def test_unknown_incident_id_fails(self, tmp_path):
        h = self._make_harness(tmp_path)
        self._seed_usage(h, incident_id="abc-123")
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": "no-such-id"}
        h.charm._on_action_show_usage(action_event)
        action_event.fail.assert_called_once()

    def test_no_cost_returns_null(self, tmp_path):
        h = self._make_harness(tmp_path)
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "increment": 1, "usage_log": [],
        }
        h.charm._status_tracker.record_usage("postgresql/0", {
            "incident_id": "abc-123",
            "timestamp": "2026-01-01T00:01:00+00:00",
            "model": "gemini-2.5-flash",
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
            # no cost_usd — Gemini does not return cost
        })
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": ""}
        h.charm._on_action_show_usage(action_event)
        call_args = action_event.set_results.call_args[0][0]
        result = json.loads(call_args["result"])
        assert result["cost_usd"] is None
        assert result["by_model"]["gemini-2.5-flash"]["cost_usd"] is None

    def test_per_model_breakdown(self, tmp_path):
        """Two calls with different models are broken down separately."""
        h = self._make_harness(tmp_path)
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked", "since": "2026-01-01T00:00:00+00:00",
            "increment": 1, "usage_log": [],
        }
        h.charm._status_tracker.record_usage("postgresql/0", {
            "incident_id": "inc-1", "timestamp": "2026-01-01T00:01:00+00:00",
            "model": "deepseek/deepseek-chat",
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.001,
        })
        h.charm._status_tracker.record_usage("postgresql/0", {
            "incident_id": "inc-2", "timestamp": "2026-01-01T00:02:00+00:00",
            "model": "gemini-2.5-flash",
            "prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280,
            "cost_usd": 0.005,
        })
        action_event = mock.MagicMock()
        action_event.params = {"incident-id": ""}
        h.charm._on_action_show_usage(action_event)
        call_args = action_event.set_results.call_args[0][0]
        result = json.loads(call_args["result"])
        assert result["total_tokens"] == 430
        assert abs(result["cost_usd"] - 0.006) < 1e-9
        assert result["by_model"]["deepseek/deepseek-chat"]["cost_usd"] == 0.001
        assert result["by_model"]["gemini-2.5-flash"]["cost_usd"] == 0.005


class TestCollectContextAction:
    def test_writes_context_bundle_and_returns_path(self, tmp_path):
        h = make_harness(tmp_path)
        h.charm._context_dir = str(tmp_path / "incidents")
        event = mock.MagicMock()

        with mock.patch.object(JaimeCharm, "_collect_report_context",
                               return_value={"unit_logs": ["line"]}), \
             mock.patch.object(JaimeCharm, "_principal_unit_name",
                               return_value="ubuntu/0"):
            h.charm._on_action_collect_context(event)

        results = event.set_results.call_args[0][0]
        assert results["unit"] == "ubuntu/0"
        assert results["context-path"].endswith("ubuntu-0-context.json")
        with open(results["context-path"]) as f:
            assert json.load(f) == {"unit_logs": ["line"]}

    def test_fails_without_principal_unit(self, tmp_path):
        h = make_harness(tmp_path)
        event = mock.MagicMock()
        with mock.patch.object(JaimeCharm, "_principal_unit_name", return_value=None):
            h.charm._on_action_collect_context(event)
        event.fail.assert_called_once()
        event.set_results.assert_not_called()
