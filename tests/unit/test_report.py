"""Unit tests for jaime.report (Markdown report generator)."""

import os

from jaime.report import generate_report

INCIDENT_ID = "550e8400-e29b-41d4-a716-446655440000"
FIRST_SEEN = "2026-07-14T09:37:54+00:00"

_FULL_CONTEXT = {
    "unit_logs": ["2026-07-14 10:00:00 ERROR some failure", "2026-07-14 10:01:00 INFO retry"],
    "systemd_failed": ["postgresql.service"],
    "disk_usage": ["Filesystem  Size  Used Avail Use% Mounted on", "/dev/sda1   20G   18G  2.0G  90% /"],
    "memory_summary": ["              total  used  free", "Mem:           7.7G  6.1G  1.6G"],
    "collected_at": "2026-07-14T10:05:00+00:00",
}

_EMPTY_CONTEXT = {
    "unit_logs": [],
    "systemd_failed": [],
    "disk_usage": [],
    "memory_summary": [],
    "collected_at": "2026-07-14T10:05:00+00:00",
}


class TestGenerateReport:
    def test_returns_path(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        assert isinstance(path, str)
        assert os.path.exists(path)

    def test_filename_is_incident_id(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        assert os.path.basename(path) == f"{INCIDENT_ID}.md"

    def test_report_contains_incident_id(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert INCIDENT_ID in content

    def test_report_contains_workload_status(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert "blocked" in content

    def test_report_contains_first_seen(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert FIRST_SEEN in content

    def test_report_contains_systemd_section(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert "postgresql.service" in content

    def test_report_contains_log_lines(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert "some failure" in content

    def test_empty_context_produces_valid_report(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _EMPTY_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert "None detected" in content or "Not available" in content or "No recent logs" in content

    def test_creates_report_dir(self, tmp_path):
        report_dir = str(tmp_path / "sub" / "reports")
        generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, report_dir)
        assert os.path.isdir(report_dir)

    def test_uses_default_dir_when_empty(self, tmp_path, monkeypatch):
        import jaime.report as jreport
        monkeypatch.setattr(jreport, "_DEFAULT_REPORT_DIR", str(tmp_path))
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, "")
        assert os.path.exists(path)

_PLAN_RESULTS_LOG_FILES = {
    "plan_results": {
        "log_files": {
            "type": "plan",
            "items": [
                {"path": "/var/log/postgresql.log", "priority": "high", "description": "Main log",
                 "status": "available", "lines": ["ERROR: connection refused"]},
            ],
        },
    },
}

_PLAN_RESULTS_PROCESSES = {
    "plan_results": {
        "processes": {
            "type": "plan",
            "items": [
                {"name": "postgres", "expected_min_count": 1, "expected_max_count": 2,
                 "running_count": 0, "status": "too_few"},
            ],
        },
    },
}

_PLAN_RESULTS_SYSTEMD = {
    "plan_results": {
        "systemd_units": {
            "type": "plan",
            "items": [
                {"unit": "postgresql.service", "status": "active"},
                {"unit": "postgresql-exporter.service", "status": "failed"},
            ],
        },
    },
}

_PLAN_RESULTS_PORTS = {
    "plan_results": {
        "network_ports": {
            "type": "plan",
            "items": [
                {"port": 5432, "protocol": "tcp", "status": "listening"},
                {"port": 8080, "protocol": "tcp", "status": "not_listening"},
            ],
        },
    },
}

_PLAN_RESULTS_ENV = {
    "plan_results": {
        "env_variables": {
            "type": "plan",
            "items": [
                {"name": "PGDATA", "value": "/var/lib/pg", "status": "set"},
                {"name": "PGPORT", "value": "", "status": "unset"},
            ],
        },
    },
}

_PLAN_RESULTS_HEALTH_COMMANDS = {
    "plan_results": {
        "health_commands": {
            "type": "plan",
            "items": [
                {"command": "systemctl is-active postgresql", "timeout_seconds": 5, "returncode": 0, "stdout": "active", "stderr": ""},
                {"command": "pgrep -f postgres", "timeout_seconds": 5, "returncode": 1, "stdout": "", "stderr": "no process found"},
            ],
        },
    },
}

_BROAD_PROCESSES = {
    "plan_results": {
        "processes": {
            "type": "broad",
            "lines": ["USER PID ...", "postgres  1234 ..."],
        },
    },
}


class TestReportPlanResults:
    def test_log_files_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_LOG_FILES}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Log files" in content
        assert "/var/log/postgresql.log" in content
        assert "connection refused" in content
        assert "✓" in content

    def test_log_files_not_found(self, tmp_path):
        ctx = {
            **_FULL_CONTEXT,
            "plan_results": {
                "log_files": {
                    "type": "plan",
                    "items": [
                        {"path": "/var/log/missing.log", "priority": "low", "description": "",
                         "status": "not_found", "lines": []},
                    ],
                },
            },
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "missing.log" in content
        assert "✗" in content

    def test_processes_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_PROCESSES}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Processes" in content
        assert "postgres" in content
        assert "too_few" in content or "✗" in content

    def test_systemd_units_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_SYSTEMD}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Systemd units" in content
        assert "postgresql.service" in content
        assert "active" in content
        assert "failed" in content

    def test_network_ports_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_PORTS}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Network ports" in content
        assert "5432" in content
        assert "8080" in content

    def test_env_variables_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_ENV}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Environment variables" in content
        assert "PGDATA" in content
        assert "PGPORT" in content

    def test_broad_processes_section(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_BROAD_PROCESSES}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Processes" in content
        assert "postgres" in content

    def test_no_plan_results_omits_extra_sections(self, tmp_path):
        ctx = {**_EMPTY_CONTEXT}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Log files" not in content
        assert "Network ports" not in content
        assert "Environment variables" not in content
        assert "Health commands" not in content

    def test_health_commands_section_success(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_HEALTH_COMMANDS}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Health commands" in content
        assert "systemctl is-active postgresql" in content
        assert "exit 0" in content
        assert "✓" in content
        assert "active" in content

    def test_health_commands_section_failure(self, tmp_path):
        ctx = {**_FULL_CONTEXT, **_PLAN_RESULTS_HEALTH_COMMANDS}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "exit 1" in content
        assert "✗" in content
        assert "no process found" in content

    def test_health_commands_not_shown_when_not_in_plan(self, tmp_path):
        ctx = {**_FULL_CONTEXT, "plan_results": {}}
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Health commands" not in content

    def test_charm_config_shows_name_and_default_only(self, tmp_path):
        import yaml
        config_yaml = yaml.dump({
            "options": {
                "port": {"type": "int", "default": 5432, "description": "Listen port"},
                "debug": {"type": "boolean", "default": False, "description": "Enable debug"},
            },
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": config_yaml, "actions_yaml": ""},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "`port`" in content
        assert "`5432`" in content
        assert "`debug`" in content
        assert "`False`" in content
        # Raw YAML content should NOT appear
        assert "Listen port" not in content
        assert "description" not in content


def _render(tmp_path, context):
    path = generate_report(
        INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, context, str(tmp_path)
    )
    with open(path) as f:
        return f.read()


class TestSnapSection:
    def test_renders_failed_snap_and_logs(self, tmp_path):
        context = {
            "snap": {
                "packages": ["postgresql 16 1 latest/stable canonical -"],
                "services": ["postgresql.primary enabled failed -"],
                "failed_changes": ["9 Error today today Start postgresql"],
                "logs": {"postgresql": ["ERROR could not start"]},
            }
        }
        report = _render(tmp_path, context)
        assert "## Snap packages" in report
        assert "## Snap services" in report
        assert "## Failed snap changes" in report
        assert "## Snap logs: `postgresql` (failed)" in report
        assert "ERROR could not start" in report

    def test_omitted_when_no_snap_context(self, tmp_path):
        assert "## Snap" not in _render(tmp_path, {"unit_logs": []})


class TestSystemdDetail:
    def test_failed_units_show_restarts_and_exit(self, tmp_path):
        context = {
            "systemd_failed": ["postgresql.service"],
            "systemd_failed_detail": [{
                "unit": "postgresql.service", "status": "failed",
                "substate": "dead", "restarts": "5", "exec_main_status": "1",
            }],
        }
        report = _render(tmp_path, context)
        assert "restarts=5" in report
        assert "exec=1" in report


class TestEnvironmentSection:
    def test_reports_set_and_unset_without_values(self, tmp_path):
        context = {"plan_results": {"env_variables": {"type": "plan", "items": [
            {"name": "PGDATA", "status": "set"},
            {"name": "PGPORT", "status": "unset"},
        ]}}}
        report = _render(tmp_path, context)
        assert "`PGDATA` — set" in report
        assert "`PGPORT` — unset" in report
        assert "values are never collected" in report


class TestPreviousLogs:
    def test_renders_previous_container_logs(self, tmp_path):
        context = {"k8s_previous_logs": ["=== container (previous): postgresql ===", "panic: oom"]}
        report = _render(tmp_path, context)
        assert "## Previous container logs" in report
        assert "panic: oom" in report


class TestContainerDetailRendering:
    def test_container_label_carries_reason_and_last_state(self, tmp_path):
        context = {"k8s_pod": {
            "name": "app-0", "phase": "Running",
            "containers": [{
                "name": "workload", "ready": False, "restartCount": 4,
                "state": "waiting", "state_reason": "CrashLoopBackOff",
                "last_state": "terminated", "last_state_reason": "OOMKilled",
                "last_exit_code": "137",
            }],
            "init_containers": [{
                "name": "init-db", "ready": True, "state": "terminated",
                "state_reason": "Completed", "exit_code": "0",
            }],
        }}
        report = _render(tmp_path, context)
        assert "CrashLoopBackOff" in report
        assert "last=terminated/OOMKilled exit=137" in report
        assert "**Init containers:**" in report
        assert "`init-db`" in report


class TestExecutiveSummary:
    def test_config_options_relabelled_not_misleading(self, tmp_path):
        config_yaml = "options:\n  port:\n    default: 5432\n"
        report = _render(tmp_path, {"charm_config": {"config_yaml": config_yaml}})
        assert "Charm options with non-empty schema defaults" in report
        assert "Explicitly enabled" not in report


class TestCharmConfigCap:
    def test_options_capped(self, tmp_path):
        options = "\n".join(f"  opt{i}:\n    default: v{i}" for i in range(150))
        config_yaml = f"options:\n{options}\n"
        report = _render(tmp_path, {"charm_config": {"config_yaml": config_yaml}})
        assert "more options omitted" in report
