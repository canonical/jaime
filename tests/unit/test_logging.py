"""Unit tests for jaime.logging (audit event writer and reader)."""

import json
import os

from jaime.logging import list_incidents, rotate_audit_log, write_event


class TestWriteEvent:
    def test_writes_json_line(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        write_event({"event": "test", "unit": "postgresql/0"}, path)
        with open(path) as f:
            line = f.readline()
        data = json.loads(line)
        assert data["event"] == "test"
        assert data["unit"] == "postgresql/0"

    def test_appends_multiple_events(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        write_event({"event": "first"}, path)
        write_event({"event": "second"}, path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "first"
        assert json.loads(lines[1])["event"] == "second"

    def test_creates_parent_directories(self, tmp_path):
        path = str(tmp_path / "sub" / "dir" / "events.jsonl")
        write_event({"event": "test"}, path)
        assert os.path.exists(path)

    def test_does_not_raise_on_unwritable_path(self):
        # Should log a warning and not raise
        write_event({"event": "test"}, "/proc/nonexistent/events.jsonl")

    def test_uses_default_path_when_empty(self, tmp_path, monkeypatch):
        import jaime.logging as jlog
        default = str(tmp_path / "events.jsonl")
        monkeypatch.setattr(jlog, "_DEFAULT_AUDIT_LOG_PATH", default)
        write_event({"event": "default-path-test"}, "")
        assert os.path.exists(default)


def _write(path, *events):
    for event in events:
        write_event(event, path)


class TestRotateAuditLog:
    def test_moves_current_file_aside_and_creates_fresh(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        write_event({"event": "incident-start", "incident_id": "i1"}, path)
        archive = rotate_audit_log(path)
        assert archive is not None
        assert os.path.exists(archive)
        assert os.path.exists(path)
        assert open(archive).read().strip()
        assert open(path).read().strip() == ""

    def test_archive_contains_original_lines(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        write_event({"event": "incident-start", "incident_id": "i1"}, path)
        write_event({"event": "incident-closed", "incident_id": "i1"}, path)
        archive = rotate_audit_log(path)
        lines = open(archive).read().splitlines()
        assert len(lines) == 2

    def test_missing_file_returns_none(self, tmp_path):
        assert rotate_audit_log(str(tmp_path / "missing.jsonl")) is None

    def test_appends_to_fresh_file_after_rotation(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        rotate_audit_log(path)  # nothing to rotate
        write_event({"event": "incident-start", "incident_id": "fresh"}, path)
        assert list_incidents(path)[0]["incident_id"] == "fresh"


class TestListIncidents:
    def test_empty_log_returns_empty(self, tmp_path):
        assert list_incidents(str(tmp_path / "missing.jsonl")) == []

    def test_open_incident_correlated(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(
            path,
            {"event": "incident-start", "unit": "postgresql/0", "workload": "blocked",
             "first_seen": "t1", "status_since": "t0", "incident_id": "i1",
             "timestamp": "2026-07-14T09:39:39+00:00"},
            {"event": "report-generated", "unit": "postgresql/0",
             "incident_id": "i1", "report_path": "/r/i1.md",
             "timestamp": "2026-07-14T09:40:00+00:00"},
        )
        incidents = list_incidents(path)
        assert len(incidents) == 1
        rec = incidents[0]
        assert rec["incident_id"] == "i1"
        assert rec["unit"] == "postgresql/0"
        assert rec["workload"] == "blocked"
        assert rec["report_path"] == "/r/i1.md"
        assert rec["closed_at"] is None
        assert rec["status"] == "open"

    def test_status_message_captured_from_incident_start(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(
            path,
            {"event": "incident-start", "unit": "openbao/0", "incident_id": "i1",
             "status_message": "Please initialize OpenBao or integrate with an auto-unseal provider",
             "timestamp": "2026-07-14T09:39:39+00:00"},
        )
        incidents = list_incidents(path)
        assert incidents[0]["status_message"] == (
            "Please initialize OpenBao or integrate with an auto-unseal provider"
        )

    def test_status_message_defaults_empty_for_old_lines(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(
            path,
            {"event": "incident-start", "unit": "a/0", "incident_id": "i1",
             "timestamp": "2026-07-14T09:39:39+00:00"},
        )
        incidents = list_incidents(path)
        assert incidents[0]["status_message"] == ""

    def test_closed_incident_status_and_order(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(
            path,
            {"event": "incident-start", "unit": "a/0", "incident_id": "old",
             "timestamp": "2026-07-13T00:00:00+00:00"},
            {"event": "incident-start", "unit": "b/0", "incident_id": "new",
             "timestamp": "2026-07-14T00:00:00+00:00"},
            {"event": "incident-closed", "unit": "a/0", "incident_id": "old",
             "closed_at": "2026-07-13T01:00:00+00:00",
             "timestamp": "2026-07-13T01:00:00+00:00"},
        )
        incidents = list_incidents(path)
        # newest (by opened_at) first
        assert [r["incident_id"] for r in incidents] == ["new", "old"]
        by_id = {r["incident_id"]: r for r in incidents}
        assert by_id["old"]["status"] == "closed"
        assert by_id["old"]["closed_at"] == "2026-07-13T01:00:00+00:00"
        assert by_id["new"]["status"] == "open"

    def test_unit_filter(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(
            path,
            {"event": "incident-start", "unit": "a/0", "incident_id": "i1",
             "timestamp": "2026-07-14T00:00:00+00:00"},
            {"event": "incident-start", "unit": "b/0", "incident_id": "i2",
             "timestamp": "2026-07-14T00:00:00+00:00"},
        )
        incidents = list_incidents(path, unit="b/0")
        assert [r["incident_id"] for r in incidents] == ["i2"]

    def test_malformed_lines_skipped(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        with open(path, "w") as f:
            f.write("{not json}\n")
            f.write(json.dumps({"event": "incident-start", "unit": "a/0",
                                "incident_id": "i1",
                                "timestamp": "2026-07-14T00:00:00+00:00"}) + "\n")
        incidents = list_incidents(path)
        assert len(incidents) == 1
        assert incidents[0]["incident_id"] == "i1"

    def test_events_without_incident_id_ignored(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        _write(path, {"event": "other", "unit": "a/0", "timestamp": "t"})
        assert list_incidents(path) == []
