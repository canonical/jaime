"""Unit tests for jaime.collector (context collection)."""

import datetime
import json
import unittest.mock as mock

import jaime.collector as jcollector
from jaime.collector import (
    _tail_lines,
    collect_context,
    collect_disk_usage,
    collect_memory_summary,
    collect_systemd_failed,
    collect_tracing_events,
    collect_unit_logs,
)


def _no_background_io():
    """Stub the collectors that shell out, so tests stay hermetic."""
    return mock.patch.multiple(
        "jaime.collector",
        collect_snap_context=mock.MagicMock(return_value={}),
        collect_ss_connections=mock.MagicMock(return_value=[]),
        collect_firewall_rules=mock.MagicMock(return_value={}),
        collect_charm_config=mock.MagicMock(return_value={}),
        _collect_systemd_failed_detail=mock.MagicMock(return_value=[]),
    )


def _make_tracing_payload(events: list[tuple[str, str, str]], ts_ns: int) -> bytes:
    """Build a minimal OTLP JSON payload for testing."""
    evt_list = []
    for name, kind, exc_type in events:
        evt_list.append({
            "name": name,
            "timeUnixNano": str(ts_ns),
            "attributes": [{"key": "kind", "value": {"stringValue": kind}}],
        })
        if exc_type:
            evt_list.append({
                "name": "exception",
                "timeUnixNano": str(ts_ns),
                "attributes": [
                    {"key": "exception.type", "value": {"stringValue": exc_type}},
                    {"key": "exception.message", "value": {"stringValue": "test error"}},
                ],
            })
    payload = {"resourceSpans": [{"scopeSpans": [{"scope": {"name": "ops"}, "spans": [{
        "name": "ops.main",
        "startTimeUnixNano": str(ts_ns),
        "events": evt_list,
    }]}]}]}
    return json.dumps(payload).encode()

class TestTailLines:
    def test_returns_all_lines_when_under_limit(self):
        lines = ["a", "b", "c"]
        assert _tail_lines("\n".join(lines), 10) == lines

    def test_returns_last_n_lines_when_over_limit(self):
        lines = [str(i) for i in range(20)]
        result = _tail_lines("\n".join(lines), 5)
        assert result == lines[-5:]

    def test_empty_string_returns_empty(self):
        assert _tail_lines("", 10) == []


class TestCollectUnitLogs:
    def test_returns_list(self, tmp_path):
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        log_file.write_text(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} ERROR some error\n"
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} INFO recovered\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60)
        assert isinstance(result, list)
        assert any("some error" in line for line in result)

    def test_missing_log_returns_empty(self, tmp_path):
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("nonexistent/0")
        assert result == []

    def test_respects_max_lines(self, tmp_path):
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        lines = "\n".join(
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} INFO line {i}" for i in range(100)
        )
        log_file.write_text(lines + "\n")
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60, max_lines=10)
        assert len(result) <= 10

    def test_filters_old_lines(self, tmp_path):
        log_file = tmp_path / "unit-postgresql-0.log"
        old_ts = "2000-01-01 00:00:00"
        now = datetime.datetime.now(datetime.timezone.utc)
        recent_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{old_ts} ERROR old line\n"
            f"{recent_ts} ERROR recent line\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60)
        assert not any("old line" in line for line in result)
        assert any("recent line" in line for line in result)

    def test_from_time_anchors_to_incident_start(self, tmp_path):
        """Logs before from_time - buffer should be excluded."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        # Line from 20 minutes ago (before incident, should be excluded)
        old_ts = (now - datetime.timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        # Incident started 3 minutes ago (from_time = now - 3min)
        from_time = now - datetime.timedelta(minutes=3)
        # Line from 6 minutes ago (within buffer of 5min before from_time → included)
        buffered_ts = (now - datetime.timedelta(minutes=7)).strftime("%Y-%m-%d %H:%M:%S")
        # Line from 1 minute ago (clearly within window → included)
        recent_ts = (now - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{old_ts} INFO pre-incident noise\n"
            f"{buffered_ts} WARNING near incident start\n"
            f"{recent_ts} ERROR incident error\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs(
                "postgresql/0", log_window_minutes=60,
                from_time=from_time, buffer_minutes=5,
            )
        assert not any("pre-incident noise" in line for line in result)
        assert any("near incident start" in line for line in result)
        assert any("incident error" in line for line in result)

    def test_from_time_not_capped_by_log_window(self, tmp_path):
        """from_time anchors the window without being capped by log_window."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        # Line from 90 minutes ago and 30 minutes ago — both within from_time - buffer
        ts_old = (now - datetime.timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
        ts_recent = (now - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(f"{ts_old} ERROR old line\n{ts_recent} ERROR recent line\n")
        from_time = now - datetime.timedelta(hours=2)
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs(
                "postgresql/0", log_window_minutes=60,
                from_time=from_time, buffer_minutes=5,
            )
        # Both lines are within from_time - buffer (2h5m ago), so both are included
        assert any("old line" in line for line in result)
        assert any("recent line" in line for line in result)

    def test_filters_to_error_warning_level(self, tmp_path):
        """Only lines with ERROR or WARNING level are matched when context_window=0."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{ts} INFO some info line\n"
            f"{ts} DEBUG some debug line\n"
            f"{ts} WARNING some warning line\n"
            f"{ts} ERROR some error line\n"
            f"{ts} INFO awaiting error resolution\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60, context_window=0)
        # ERROR and WARNING lines should be included
        assert any("some warning line" in line for line in result)
        assert any("some error line" in line for line in result)
        # INFO and DEBUG lines should NOT be included (context_window=0)
        assert not any("some info line" in line for line in result)
        assert not any("some debug line" in line for line in result)
        # INFO lines containing "error" in message body but INFO level should NOT be matched
        assert not any("awaiting error resolution" in line for line in result)

    def test_no_error_warning_matches_falls_back(self, tmp_path):
        """When no lines match ERROR/WARNING level, all recent lines are returned."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{ts} INFO line one\n"
            f"{ts} INFO line two\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60)
        assert any("line one" in line for line in result)
        assert any("line two" in line for line in result)

    def test_context_window_around_last_match(self, tmp_path):
        """Lines within context_window of the last match are included."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        # Use varied, realistic log messages so _deduplicate_lines does not
        # collapse them (uniform synthetic messages would share the same
        # structural pattern after token stripping and trigger deduplication).
        messages = [
            "juju.worker started agent loop",
            "juju.api connected to controller",
            "juju.worker waiting for lock",
            "juju.runner dispatching hook install",
            "juju.state loading charm config",
            "juju.storage attaching volume",
            "juju.network interface eth0 up",
            "juju.worker dispatching hook config-changed",
            "juju.state writing unit data",
            "juju.api refreshing credentials",
            "juju.runner hook config-changed finished",
            "juju.worker checking relation data",  # index 11 — outside window
            "juju.state relation joined postgresql",  # index 12 — inside window
            "juju.api querying application status",  # index 13 — inside window
            "juju.worker processing pending events",  # index 14 — inside window
            "connection to database lost unexpectedly",  # index 15 — ERROR match
            "juju.runner retrying failed hook",  # index 16 — inside window
            "juju.state rollback initiated for unit",  # index 17 — inside window
            "juju.worker recovery attempt started",  # index 18 — inside window
            "juju.api notified controller of failure",
        ]
        lines = []
        for i, msg in enumerate(messages):
            ts = (now - datetime.timedelta(minutes=20 - i)).strftime("%Y-%m-%d %H:%M:%S")
            level = "ERROR" if i == 15 else "INFO"
            lines.append(f"{ts} {level} {msg}")
        log_file.write_text("\n".join(lines) + "\n")
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60, context_window=3)
        # The ERROR at index 15 should be included
        assert any("connection to database lost" in line for line in result)
        # Context window around index 15: indices 12-18
        assert any("relation joined postgresql" in line for line in result)   # 12
        assert any("querying application status" in line for line in result)  # 13
        assert any("processing pending events" in line for line in result)    # 14
        assert any("retrying failed hook" in line for line in result)         # 16
        # Line at index 11 (outside context_window) should not be included
        assert not any("checking relation data" in line for line in result)   # 11

    def test_context_window_does_not_exceed_bounds(self, tmp_path):
        """Context window at the start/end of recent lines doesn't go out of bounds."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        ts0 = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        ts1 = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{ts0} WARNING first line\n"
            f"{ts1} ERROR last line\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60, context_window=5)
        # Both lines should be included (context window at the edges)
        assert len(result) == 2

    def test_without_from_time_uses_rolling_window(self, tmp_path):
        """Without from_time, original rolling window behaviour is preserved."""
        log_file = tmp_path / "unit-postgresql-0.log"
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
        recent_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file.write_text(
            f"{old_ts} ERROR old rolling line\n"
            f"{recent_ts} ERROR new rolling line\n"
        )
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = collect_unit_logs("postgresql/0", log_window_minutes=60)
        assert not any("old rolling line" in line for line in result)
        assert any("new rolling line" in line for line in result)


class TestCollectSystemdFailed:
    def test_returns_list(self):
        with mock.patch("jaime.collector._run", return_value="nginx.service\npostgresql.service\n"):
            result = collect_systemd_failed()
        assert result == ["nginx.service", "postgresql.service"]

    def test_returns_empty_when_no_failures(self):
        with mock.patch("jaime.collector._run", return_value=""):
            result = collect_systemd_failed()
        assert result == []


class TestCollectDiskUsage:
    def test_returns_lines(self):
        output = "Filesystem  Size  Used Avail Use% Mounted on\n/dev/sda1   20G  18G  2G   90% /\n"
        with mock.patch("jaime.collector._run", return_value=output):
            result = collect_disk_usage()
        assert len(result) == 2
        assert "Filesystem" in result[0]


class TestCollectMemorySummary:
    def test_returns_lines(self):
        output = "              total  used  free\nMem:           7.7G  6.1G  1.6G\n"
        with mock.patch("jaime.collector._run", return_value=output):
            result = collect_memory_summary()
        assert any("Mem" in line for line in result)


class TestCollectContext:
    def test_returns_all_sections(self):
        with _no_background_io(), \
             mock.patch("jaime.collector.collect_unit_logs", return_value=["log line"]), \
             mock.patch("jaime.collector.collect_systemd_failed", return_value=[]), \
             mock.patch("jaime.collector.collect_disk_usage", return_value=["disk line"]), \
             mock.patch("jaime.collector.collect_memory_summary", return_value=["mem line"]):
            ctx = collect_context("postgresql/0")
        assert "unit_logs" in ctx
        assert "systemd_failed" in ctx
        assert "disk_usage" in ctx
        assert "memory_summary" in ctx
        assert "charm_config" in ctx
        assert "collected_at" in ctx

    def test_collected_at_is_utc_iso(self):
        with _no_background_io(), \
             mock.patch("jaime.collector.collect_unit_logs", return_value=[]), \
             mock.patch("jaime.collector.collect_systemd_failed", return_value=[]), \
             mock.patch("jaime.collector.collect_disk_usage", return_value=[]), \
             mock.patch("jaime.collector.collect_memory_summary", return_value=[]):
            ctx = collect_context("postgresql/0")
        assert "+00:00" in ctx["collected_at"] or ctx["collected_at"].endswith("Z")


class TestCollectCharmConfig:
    def test_returns_config_and_actions(self):
        config_yaml = "options:\n  port:\n    default: 5432\n"
        actions_yaml = "actions:\n  restart:\n    description: Restart\n"
        contents = {
            "/var/lib/juju/agents/unit-postgresql-0/charm/config.yaml": config_yaml,
            "/var/lib/juju/agents/unit-postgresql-0/charm/actions.yaml": actions_yaml,
        }
        import jaime.collector as jcollector
        with mock.patch("builtins.open", mock.mock_open()) as m:
            m.side_effect = lambda p, *a, **kw: mock.mock_open(read_data=contents.get(p, ""))()
            result = jcollector.collect_charm_config("postgresql/0")
        assert result["config_yaml"] == config_yaml
        assert result["actions_yaml"] == actions_yaml

    def test_missing_files_returns_empty(self):
        import jaime.collector as jcollector
        result = jcollector.collect_charm_config("nonexistent/0")
        assert result["config_yaml"] == ""
        assert result["actions_yaml"] == ""


class TestCollectTracingEvents:
    def _make_db(self, tmp_path, rows: list) -> str:
        import sqlite3 as _sq
        db_path = str(tmp_path / ".tracing-data.db")
        conn = _sq.connect(db_path)
        conn.execute("CREATE TABLE tracing (id INTEGER PRIMARY KEY AUTOINCREMENT, state INTEGER, data BLOB, content_type TEXT)")
        for data in rows:
            conn.execute("INSERT INTO tracing (state, data, content_type) VALUES (50, ?, 'application/json')", (data,))
        conn.commit()
        conn.close()
        return db_path

    def _unit_tag(self, unit_name: str) -> str:
        return "unit-" + unit_name.replace("/", "-")

    def test_returns_events_from_db(self, tmp_path):
        ts_ns = int(datetime.datetime(2026, 7, 14, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1e9)
        data = _make_tracing_payload([("RelationBrokenEvent", "relation_broken", ""), ("ConfigChangedEvent", "config_changed", "")], ts_ns)
        db_path = self._make_db(tmp_path, [data])
        with mock.patch("jaime.collector.sqlite3.connect", return_value=__import__("sqlite3").connect(db_path)):
            result = collect_tracing_events("postgresql/0")
        assert any(e["event"] == "RelationBrokenEvent" for e in result)
        assert any(e["event"] == "ConfigChangedEvent" for e in result)

    def test_attaches_exception_to_previous_event(self, tmp_path):
        ts_ns = int(datetime.datetime(2026, 7, 14, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1e9)
        data = _make_tracing_payload([("RelationBrokenEvent", "relation_broken", "ValueError")], ts_ns)
        db_path = self._make_db(tmp_path, [data])
        with mock.patch("jaime.collector.sqlite3.connect", return_value=__import__("sqlite3").connect(db_path)):
            result = collect_tracing_events("postgresql/0")
        assert len(result) == 1
        assert result[0]["exception_type"] == "ValueError"
        assert result[0]["exception_message"] == "test error"

    def test_returns_empty_when_db_missing(self):
        result = collect_tracing_events("nonexistent/0")
        assert result == []

    def test_filters_by_from_time(self, tmp_path):
        old_ts_ns = int(datetime.datetime(2026, 7, 14, 9, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1e9)
        new_ts_ns = int(datetime.datetime(2026, 7, 14, 11, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1e9)
        old_data = _make_tracing_payload([("InstallEvent", "install", "")], old_ts_ns)
        new_data = _make_tracing_payload([("ConfigChangedEvent", "config_changed", "")], new_ts_ns)
        db_path = self._make_db(tmp_path, [old_data, new_data])
        from_time = datetime.datetime(2026, 7, 14, 10, 0, 0, tzinfo=datetime.timezone.utc)
        with mock.patch("jaime.collector.sqlite3.connect", return_value=__import__("sqlite3").connect(db_path)):
            result = collect_tracing_events("postgresql/0", from_time=from_time)
        assert not any(e["event"] == "InstallEvent" for e in result)
        assert any(e["event"] == "ConfigChangedEvent" for e in result)

    def test_skips_commit_events(self, tmp_path):
        ts_ns = int(datetime.datetime(2026, 7, 14, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1e9)
        data = _make_tracing_payload([
            ("UpdateStatusEvent", "update_status", ""),
            ("PreCommitEvent", "pre_commit", ""),
            ("CommitEvent", "commit", ""),
        ], ts_ns)
        db_path = self._make_db(tmp_path, [data])
        with mock.patch("jaime.collector.sqlite3.connect", return_value=__import__("sqlite3").connect(db_path)):
            result = collect_tracing_events("postgresql/0")
        assert not any(e["event"] in ("PreCommitEvent", "CommitEvent") for e in result)


    def test_available_file_returns_lines(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("line1\nline2\nline3\n")
        plan_lf = [{"path": str(log), "priority": "high", "description": "test log"}]
        import jaime.collector as jcollector
        with mock.patch.object(jcollector, "_JUJU_LOG_DIR", str(tmp_path)):
            result = jcollector._collect_log_files(plan_lf, 100)
        assert len(result) == 1
        assert result[0]["status"] == "available"
        assert "line2" in result[0]["lines"]

    def test_not_found_file_returns_not_found(self):
        plan_lf = [{"path": "/nonexistent/path.log", "priority": "low", "description": ""}]
        result = jcollector._collect_log_files(plan_lf, 100)
        assert result[0]["status"] == "not_found"
        assert result[0]["lines"] == []

    def test_empty_plan_returns_empty(self):
        assert jcollector._collect_log_files([], 100) == []


class TestCollectProcesses:
    def test_process_running(self):
        with mock.patch("jaime.collector._run", return_value="12345\n"):
            result = jcollector._collect_processes([{"name": "nginx", "expected_min_count": 1, "expected_max_count": 2}])
        assert result[0]["status"] == "ok"
        assert result[0]["running_count"] == 1

    def test_too_few_processes(self):
        with mock.patch("jaime.collector._run", return_value=""):
            result = jcollector._collect_processes([{"name": "nginx", "expected_min_count": 1, "expected_max_count": 2}])
        assert result[0]["status"] == "too_few"
        assert result[0]["running_count"] == 0

    def test_too_many_processes(self):
        with mock.patch("jaime.collector._run", return_value="12345\n12346\n12347\n"):
            result = jcollector._collect_processes([{"name": "nginx", "expected_min_count": 1, "expected_max_count": 2}])
        assert result[0]["status"] == "too_many"
        assert result[0]["running_count"] == 3


class TestCollectNetworkPorts:
    def test_port_listening(self):
        result = jcollector._collect_network_ports(
            [{"port": 5432, "protocol": "tcp"}], "tcp LISTEN 0 128 0.0.0.0:5432"
        )
        assert result[0]["status"] == "listening"

    def test_port_not_listening(self):
        result = jcollector._collect_network_ports(
            [{"port": 5432, "protocol": "tcp"}], "tcp LISTEN 0 128 0.0.0.0:80"
        )
        assert result[0]["status"] == "not_listening"

    def test_port_no_false_positive_on_substring_match(self):
        """Port 80 must not match 8080, 8000, or 54320 in the ss output."""
        output = "tcp LISTEN 0 128 0.0.0.0:8080\ntcp LISTEN 0 128 0.0.0.0:8000\ntcp LISTEN 0 128 0.0.0.0:54320"
        result = jcollector._collect_network_ports([{"port": 80, "protocol": "tcp"}], output)
        assert result[0]["status"] == "not_listening"


class TestBroadPorts:
    def test_derives_listening_ports_from_single_ss_collection(self):
        ss = "tcp LISTEN 0 128 0.0.0.0:5432\ntcp ESTAB 0 0 1.2.3.4:5432"
        result = jcollector._collect_broad_ports(ss, 100)
        assert result == ["tcp LISTEN 0 128 0.0.0.0:5432"]


class TestCollectHealthCommands:
    def test_command_succeeds(self):
        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active\n"
        mock_result.stderr = ""
        with mock.patch("jaime.collector.subprocess.run", return_value=mock_result):
            result = jcollector._collect_health_commands([
                {"command": "systemctl is-active postgresql", "timeout_seconds": 5},
            ])
        assert len(result) == 1
        assert result[0]["returncode"] == 0
        assert result[0]["stdout"] == "active"
        assert result[0]["stderr"] == ""

    def test_command_fails(self):
        mock_result = mock.MagicMock()
        mock_result.returncode = 3
        mock_result.stdout = ""
        mock_result.stderr = "Unit not found"
        with mock.patch("jaime.collector.subprocess.run", return_value=mock_result):
            result = jcollector._collect_health_commands([
                {"command": "systemctl is-active nonexistent", "timeout_seconds": 5},
            ])
        assert result[0]["returncode"] == 3
        assert result[0]["stderr"] == "Unit not found"

    def test_command_times_out(self):
        with mock.patch("jaime.collector.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("cmd", 5)):
            result = jcollector._collect_health_commands([
                {"command": "sleep 100", "timeout_seconds": 5},
            ])
        assert result[0]["returncode"] == -1
        assert "timed out" in result[0]["stderr"]

    def test_command_raises_exception(self):
        with mock.patch("jaime.collector.subprocess.run", side_effect=FileNotFoundError("no such binary")):
            result = jcollector._collect_health_commands([
                {"command": "nonexistent-binary", "timeout_seconds": 5},
            ])
        assert result[0]["returncode"] == -1
        assert "no such binary" in result[0]["stderr"]

    def test_empty_plan_returns_empty(self):
        assert jcollector._collect_health_commands([]) == []


class TestCollectEnvVariables:
    def test_var_set_never_reports_value(self):
        result = jcollector._collect_env_variables(["PATH"])
        assert result[0]["status"] == "set"
        assert "value" not in result[0]

    def test_var_unset(self):
        result = jcollector._collect_env_variables(["SOME_UNDEFINED_VAR_XYZ"])
        assert result[0]["status"] == "unset"
        assert "value" not in result[0]


class TestCollectContextWithPlan:
    def test_plan_driven_collects_all_sections(self):
        plan = {
            "principal_name": "postgresql",
            "monitoring_plan": {
                "log_files": [{"path": "/var/log/postgresql.log", "priority": "high", "description": "main log"}],
                "processes": [{"name": "postgres", "expected_min_count": 1, "expected_max_count": 2}],
                "systemd_units": ["postgresql.service"],
                "env_variables": ["PGDATA"],
                "network": {"ports": [{"port": 5432, "protocol": "tcp"}]},
                "health_commands": [{"command": "systemctl is-active postgresql", "timeout_seconds": 5}],
            },
        }
        mocks = {
            "collect_unit_logs": mock.MagicMock(return_value=[]),
            "collect_disk_usage": mock.MagicMock(return_value=[]),
            "collect_memory_summary": mock.MagicMock(return_value=[]),
            "collect_systemd_failed": mock.MagicMock(return_value=[]),
            "_collect_log_files": mock.MagicMock(return_value=[{"path": "/var/log/postgresql.log", "status": "available", "lines": ["log line"]}]),
            "_collect_processes": mock.MagicMock(return_value=[{"name": "postgres", "status": "ok"}]),
            "_collect_systemd_units": mock.MagicMock(return_value=[{"unit": "postgresql.service", "status": "active"}]),
            "_collect_network_ports": mock.MagicMock(return_value=[{"port": 5432, "protocol": "tcp", "status": "listening"}]),
            "_collect_env_variables": mock.MagicMock(return_value=[{"name": "PGDATA", "value": "/var/lib/pg", "status": "set"}]),
            "_collect_health_commands": mock.MagicMock(return_value=[{"command": "systemctl is-active postgresql", "returncode": 0, "stdout": "active", "stderr": ""}]),
        }
        with _no_background_io(), mock.patch.multiple("jaime.collector", **mocks):
            ctx = collect_context("postgresql/0", diagnostics_plan=plan)
        pr = ctx["plan_results"]
        assert pr["log_files"]["type"] == "plan"
        assert pr["processes"]["type"] == "plan"
        assert pr["systemd_units"]["type"] == "plan"
        assert pr["network_ports"]["type"] == "plan"
        assert pr["env_variables"]["type"] == "plan"
        assert pr["health_commands"]["type"] == "plan"
        assert pr["health_commands"]["items"][0]["returncode"] == 0

    def test_plan_with_empty_processes_falls_back_to_broad(self):
        """When a plan section is empty, broad fallback is used instead."""
        plan = {
            "principal_name": "postgresql",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "systemd_units": [],
                "env_variables": [],
                "network": {},
            },
        }
        mocks = {
            "collect_unit_logs": mock.MagicMock(return_value=[]),
            "collect_disk_usage": mock.MagicMock(return_value=[]),
            "collect_memory_summary": mock.MagicMock(return_value=[]),
            "collect_systemd_failed": mock.MagicMock(return_value=["failed.service"]),
            "_collect_broad_processes": mock.MagicMock(return_value=["ps line 1", "ps line 2"]),
            "_collect_broad_ports": mock.MagicMock(return_value=["ss line 1"]),
        }
        with _no_background_io(), mock.patch.multiple("jaime.collector", **mocks):
            ctx = collect_context("postgresql/0", diagnostics_plan=plan)
        pr = ctx["plan_results"]
        assert pr["processes"]["type"] == "broad"
        assert pr["network_ports"]["type"] == "broad"
        assert pr["systemd_units"]["type"] == "broad"

    def test_no_plan_falls_back_to_broad(self):
        mocks = {
            "collect_unit_logs": mock.MagicMock(return_value=[]),
            "collect_disk_usage": mock.MagicMock(return_value=[]),
            "collect_memory_summary": mock.MagicMock(return_value=[]),
            "collect_systemd_failed": mock.MagicMock(return_value=["failed.service"]),
            "_collect_broad_processes": mock.MagicMock(return_value=["ps line"]),
            "_collect_broad_ports": mock.MagicMock(return_value=["ss line"]),
        }
        with _no_background_io(), mock.patch.multiple("jaime.collector", **mocks):
            ctx = collect_context("postgresql/0", diagnostics_plan=None)
        pr = ctx["plan_results"]
        assert pr["processes"]["type"] == "broad"
        assert pr["network_ports"]["type"] == "broad"
        assert "systemd_failed" in ctx


class TestLineCaps:
    def test_cap_lines_truncates_a_pathological_line(self):
        from jaime.logutils import cap_lines
        huge = "x" * (10 * 1024)
        result = cap_lines([huge], 10)
        assert len(result) == 1
        assert "truncated" in result[0]
        assert len(result[0]) < len(huge)

    def test_cap_lines_tail_bounds(self):
        from jaime.logutils import cap_lines
        assert cap_lines([str(i) for i in range(100)], 5) == ["95", "96", "97", "98", "99"]


class TestErrorWindow:
    def test_keeps_latest_error_with_context(self):
        lines = [f"line {i}" for i in range(30)] + ["ERROR boom"] + [f"after {i}" for i in range(15)]
        result = jcollector._error_window(lines, 10, 100)
        assert "ERROR boom" in result
        assert "line 20" in result      # 10 before
        assert "after 4" in result      # 10 after
        assert "line 19" not in result

    def test_falls_back_to_tail_without_error(self):
        lines = [f"info {i}" for i in range(50)]
        assert jcollector._error_window(lines, 10, 5) == [f"info {i}" for i in range(45, 50)]


class TestSnapContext:
    def test_no_snap_returns_empty(self):
        with mock.patch("jaime.collector.shutil.which", return_value=None):
            assert jcollector.collect_snap_context(100) == {}

    def test_healthy_host_omits_snap_section(self):
        def fake_run(cmd, timeout=10):
            if cmd[:2] == ["snap", "list"]:
                return "Name Version Rev Tracking Publisher Notes\ncore22 1 1 latest/stable canonical -\n"
            if cmd[:2] == ["snap", "services"]:
                return "Name Startup Current Notes\ncore22.daemon enabled active -\n"
            if cmd[:2] == ["snap", "changes"]:
                return "ID Status Spawn Ready Summary\n1 Done today today Do things\n"
            return ""

        with mock.patch("jaime.collector.shutil.which", return_value="/usr/bin/snap"), \
             mock.patch("jaime.collector._run", side_effect=fake_run):
            assert jcollector.collect_snap_context(100) == {}

    def test_failed_service_yields_status_and_windowed_logs(self):
        def fake_run(cmd, timeout=10):
            if cmd[:2] == ["snap", "list"]:
                return "Name Version Rev Tracking Publisher Notes\npostgresql 16 1 latest/stable canonical -\n"
            if cmd[:2] == ["snap", "services"]:
                return "Name Startup Current Notes\npostgresql.primary enabled failed -\n"
            if cmd[:2] == ["snap", "changes"]:
                return "ID Status Spawn Ready Summary\n9 Error today today Start postgresql\n"
            if cmd[:2] == ["snap", "logs"]:
                return "\n".join([f"info {i}" for i in range(20)] + ["ERROR could not start"] + [f"tail {i}" for i in range(5)])
            return ""

        with mock.patch("jaime.collector.shutil.which", return_value="/usr/bin/snap"), \
             mock.patch("jaime.collector._run", side_effect=fake_run):
            ctx = jcollector.collect_snap_context(100)
        assert ctx["failed_changes"] == ["9 Error today today Start postgresql"]
        assert any("postgresql.primary" in line for line in ctx["services"])
        assert any("ERROR could not start" in line for line in ctx["logs"]["postgresql"])


class TestSystemdDetail:
    def test_parses_show_output(self):
        output = "ActiveState=failed\nSubState=dead\nNRestarts=5\nExecMainStatus=1\n"
        with mock.patch("jaime.collector._run", return_value=output):
            result = jcollector._collect_systemd_units(["postgresql.service"])
        assert result[0]["status"] == "failed"
        assert result[0]["substate"] == "dead"
        assert result[0]["restarts"] == "5"
        assert result[0]["exec_main_status"] == "1"


class TestBoundsOnPreviouslyUnbounded:
    def test_firewall_rules_capped_per_table(self):
        output = "\n".join(f"rule {i}" for i in range(500)) + "\n"
        with mock.patch("jaime.collector._run", return_value=output):
            result = jcollector.collect_firewall_rules(max_lines=25)
        assert all(len(v) <= 25 for v in result.values())
        assert result  # at least one table present

    def test_ss_collected_without_sudo_and_capped(self):
        calls = []

        def fake_run(cmd, timeout=10):
            calls.append(cmd)
            return "\n".join(f"tcp LISTEN 0 0 0.0.0.0:{i}" for i in range(100))

        with mock.patch("jaime.collector._run", side_effect=fake_run):
            result = jcollector.collect_ss_connections(max_lines=10)
        assert calls == [["ss", "-antlup"]]
        assert "sudo" not in calls[0]
        assert len(result) == 10

    def test_systemd_failed_capped(self):
        output = "\n".join(f"unit{i}.service" for i in range(100)) + "\n"
        with mock.patch("jaime.collector._run", return_value=output):
            result = jcollector.collect_systemd_failed(max_lines=7)
        assert len(result) == 7

    def test_plan_item_counts_capped(self):
        plan = {"principal_name": "x", "monitoring_plan": {
            "processes": [{"name": f"p{i}"} for i in range(100)],
            "systemd_units": [f"u{i}.service" for i in range(100)],
        }}
        with _no_background_io(), \
             mock.patch("jaime.collector._collect_processes", side_effect=lambda items: items), \
             mock.patch("jaime.collector._collect_systemd_units", side_effect=lambda items: items), \
             mock.patch("jaime.collector.collect_unit_logs", return_value=[]), \
             mock.patch("jaime.collector.collect_systemd_failed", return_value=[]):
            ctx = collect_context("x/0", diagnostics_plan=plan)
        assert len(ctx["plan_results"]["processes"]["items"]) == jcollector._PLAN_ITEM_CAPS["processes"]
        assert len(ctx["plan_results"]["systemd_units"]["items"]) == jcollector._PLAN_ITEM_CAPS["systemd_units"]
