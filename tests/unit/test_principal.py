"""Unit tests for StatusTracker in jaime.principal."""

import json
import os

import pytest

from jaime.principal import StatusTracker

SINCE_A = "2026-07-13T10:00:00+00:00"
SINCE_B = "2026-07-13T11:00:00+00:00"
TS_1 = "2026-07-13T10:05:00+00:00"
TS_2 = "2026-07-13T10:35:00+00:00"


@pytest.fixture
def tracker(tmp_path):
    return StatusTracker(state_path=str(tmp_path / "status-state.json"))


class TestObserve:
    def test_first_observation_returns_increment_one(self, tracker):
        assert tracker.observe("postgresql/0", "blocked", SINCE_A) == 1

    def test_same_status_and_since_increments(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        assert tracker.observe("postgresql/0", "blocked", SINCE_A) == 3

    def test_recovery_resets_increment(self, tracker):
        """Leaving the watched statuses ends the episode."""
        tracker.observe("postgresql/0", "blocked", SINCE_A, watched=True)
        tracker.observe("postgresql/0", "blocked", SINCE_A, watched=True)
        assert tracker.observe("postgresql/0", "active", SINCE_B, watched=False) == 1

    def test_same_status_new_since_continues_episode(self, tracker):
        """Juju bumps `since` whenever a charm re-sets its status.

        That is not a new episode — a charm looping on the same failure with a
        changing message must not reset the unhealthy timer.
        """
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        assert tracker.observe("postgresql/0", "blocked", SINCE_B) == 3

    def test_flapping_between_watched_statuses_continues_episode(self, tracker):
        """maintenance -> blocked -> maintenance is one continuous episode."""
        assert tracker.observe("postgresql/0", "maintenance", SINCE_A, watched=True) == 1
        assert tracker.observe("postgresql/0", "blocked", SINCE_B, watched=True) == 2
        assert tracker.observe("postgresql/0", "maintenance", SINCE_A, watched=True) == 3

    def test_unhealthy_since_anchored_at_episode_start(self, tracker):
        """The anchor is the first `since` seen, not the latest one."""
        tracker.observe("postgresql/0", "maintenance", SINCE_A, watched=True)
        tracker.observe("postgresql/0", "blocked", SINCE_B, watched=True)
        assert tracker.unhealthy_since("postgresql/0") == SINCE_A

    def test_unhealthy_since_cleared_on_recovery(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A, watched=True)
        tracker.observe("postgresql/0", "active", SINCE_B, watched=False)
        assert tracker.unhealthy_since("postgresql/0") is None

    def test_unhealthy_since_re_anchored_on_new_episode(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A, watched=True)
        tracker.observe("postgresql/0", "active", SINCE_A, watched=False)
        tracker.observe("postgresql/0", "blocked", SINCE_B, watched=True)
        assert tracker.unhealthy_since("postgresql/0") == SINCE_B

    def test_unhealthy_since_none_for_unknown_unit(self, tracker):
        assert tracker.unhealthy_since("nope/0") is None

    def test_same_status_new_since_preserves_last_reported(self, tracker):
        """A `since` bump must not clear the cooldown bookkeeping."""
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        assert tracker.last_reported("postgresql/0") == TS_1

        tracker.observe("postgresql/0", "blocked", SINCE_B)
        assert tracker.last_reported("postgresql/0") == TS_1
        assert tracker.current_incident("postgresql/0") == INCIDENT_1

    def test_flapping_preserves_open_incident(self, tracker):
        tracker.observe("postgresql/0", "maintenance", SINCE_A, watched=True)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        tracker.observe("postgresql/0", "blocked", SINCE_B, watched=True)
        assert tracker.has_open_incident("postgresql/0")
        assert tracker.last_reported("postgresql/0") == TS_1

    def test_recovery_clears_last_reported(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A, watched=True)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        tracker.observe("postgresql/0", "active", SINCE_B, watched=False)
        assert tracker.last_reported("postgresql/0") is None

    def test_increment_survives_a_reload(self, tracker, tmp_path):
        """Each hook is a fresh process, so increments must be persisted."""
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        for expected in (2, 3, 4):
            reloaded = StatusTracker(state_path=tracker._path)
            assert reloaded.observe("postgresql/0", "blocked", SINCE_A) == expected

    def test_multiple_units_are_independent(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.observe("mysql/0", "blocked", SINCE_A)
        assert tracker.observe("postgresql/0", "blocked", SINCE_A) == 3
        assert tracker.observe("mysql/0", "blocked", SINCE_A) == 2


INCIDENT_1 = {"id": "aaaaaaaa-0000-0000-0000-000000000001", "opened_at": TS_1}
INCIDENT_2 = {"id": "aaaaaaaa-0000-0000-0000-000000000002", "opened_at": TS_2}


class TestRecordReported:
    def test_sets_last_reported(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        assert tracker.last_reported("postgresql/0") == TS_1

    def test_stores_incident(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        assert tracker.current_incident("postgresql/0") == INCIDENT_1

    def test_overwrites_last_reported(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        tracker.record_reported("postgresql/0", TS_2, INCIDENT_2)
        assert tracker.last_reported("postgresql/0") == TS_2
        assert tracker.current_incident("postgresql/0") == INCIDENT_2

    def test_no_op_for_unknown_unit(self, tracker):
        tracker.record_reported("unknown/0", TS_1, INCIDENT_1)
        assert tracker.last_reported("unknown/0") is None

    def test_does_not_affect_increment(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        assert tracker.observe("postgresql/0", "blocked", SINCE_A) == 3


class TestLastReported:
    def test_returns_none_for_unknown_unit(self, tracker):
        assert tracker.last_reported("unknown/0") is None

    def test_returns_none_before_any_report(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        assert tracker.last_reported("postgresql/0") is None


class TestCloseIncident:
    def test_has_open_incident_false_when_no_incident(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        assert tracker.has_open_incident("postgresql/0") is False

    def test_has_open_incident_true_after_record_reported(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        assert tracker.has_open_incident("postgresql/0") is True

    def test_has_open_incident_false_after_close(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        closed = {**INCIDENT_1, "closed_at": TS_2}
        tracker.close_incident("postgresql/0", closed)
        assert tracker.has_open_incident("postgresql/0") is False

    def test_close_incident_stores_closed_dict(self, tracker):
        tracker.observe("postgresql/0", "blocked", SINCE_A)
        tracker.record_reported("postgresql/0", TS_1, INCIDENT_1)
        closed = {**INCIDENT_1, "closed_at": TS_2}
        tracker.close_incident("postgresql/0", closed)
        assert tracker.current_incident("postgresql/0")["closed_at"] == TS_2

    def test_has_open_incident_false_for_unknown_unit(self, tracker):
        assert tracker.has_open_incident("unknown/0") is False


class TestPersistence:
    def test_state_is_saved_to_disk(self, tmp_path):
        path = str(tmp_path / "state.json")
        t = StatusTracker(state_path=path)
        t.observe("postgresql/0", "blocked", SINCE_A)
        assert os.path.exists(path)
        with open(path) as f:
            state = json.load(f)
        assert state["postgresql/0"]["status"] == "blocked"
        assert state["postgresql/0"]["increment"] == 1

    def test_state_is_loaded_from_disk(self, tmp_path):
        path = str(tmp_path / "state.json")
        t1 = StatusTracker(state_path=path)
        t1.observe("postgresql/0", "blocked", SINCE_A)
        t1.observe("postgresql/0", "blocked", SINCE_A)
        t1.record_reported("postgresql/0", TS_1, INCIDENT_1)

        # New instance reads the same file
        t2 = StatusTracker(state_path=path)
        assert t2.observe("postgresql/0", "blocked", SINCE_A) == 3
        assert t2.last_reported("postgresql/0") == TS_1

    def test_missing_file_starts_empty(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        t = StatusTracker(state_path=path)
        assert t.observe("postgresql/0", "blocked", SINCE_A) == 1

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            f.write("not valid json{{{")
        t = StatusTracker(state_path=path)
        assert t.observe("postgresql/0", "blocked", SINCE_A) == 1

    def test_reset_clears_all_state(self, tmp_path):
        path = str(tmp_path / "state.json")
        t = StatusTracker(state_path=path)
        t.observe("postgresql/0", "blocked", SINCE_A)
        t.record_reported("postgresql/0", TS_1, INCIDENT_1)
        t.record_usage("postgresql/0", {"incident_id": "x", "timestamp": TS_1, "total_tokens": 10})

        t.reset()

        assert t._state == {}
        assert t.last_reported("postgresql/0") is None
        assert t.all_usage_log() == []
        # Persisted empty state survives a reload.
        t2 = StatusTracker(state_path=path)
        assert t2._state == {}


class TestIncidentAccessors:
    def _seed(self, tracker):
        tracker.observe("a/0", "blocked", SINCE_A)
        tracker.record_reported("a/0", TS_1, {"id": "open-1", "opened_at": TS_1})
        tracker.observe("b/0", "blocked", SINCE_A)
        tracker.record_reported("b/0", TS_1, {"id": "closed-1", "opened_at": TS_1, "closed_at": TS_2})
        tracker.observe("c/0", "active", SINCE_A, watched=False)

    def test_units_and_entries(self, tracker):
        self._seed(tracker)
        assert tracker.units() == ["a/0", "b/0", "c/0"]
        assert [u for u, _ in tracker.entries()] == ["a/0", "b/0", "c/0"]

    def test_incidents_default_to_open_ones(self, tracker):
        self._seed(tracker)
        assert [(u, i["id"]) for u, _e, i in tracker.incidents()] == [("a/0", "open-1")]

    def test_incidents_can_include_closed(self, tracker):
        self._seed(tracker)
        ids = [i["id"] for _u, _e, i in tracker.incidents(include_closed=True)]
        assert ids == ["open-1", "closed-1"]

    def test_incident_id_matches_open_or_closed(self, tracker):
        self._seed(tracker)
        assert [i["id"] for _u, _e, i in tracker.incidents("closed-1")] == ["closed-1"]
        assert tracker.incidents("nope") == []

    def test_path_property(self, tracker, tmp_path):
        assert tracker.path == str(tmp_path / "status-state.json")
