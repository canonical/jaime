"""Unit tests for jaime.report (Markdown report generator)."""

import os

from jaime.report import generate_report
from jaime.suggest import build_suggest_prompt

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

    def test_status_message_rendered_in_header(self, tmp_path):
        message = "Please initialize OpenBao or integrate with an auto-unseal provider"
        path = generate_report(
            INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT,
            str(tmp_path), status_message=message,
        )
        content = open(path).read()
        assert f"- status-message: {message}" in content

    def test_status_message_rendered_in_summary(self, tmp_path):
        message = "Please initialize OpenBao or integrate with an auto-unseal provider"
        path = generate_report(
            INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT,
            str(tmp_path), status_message=message,
        )
        content = open(path).read()
        assert f"Status message: {message}" in content

    def test_no_status_message_omits_line(self, tmp_path):
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, _FULL_CONTEXT, str(tmp_path))
        content = open(path).read()
        assert "status-message:" not in content

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

    def test_charm_config_omits_empty_defaults_with_marker(self, tmp_path):
        import yaml
        config_yaml = yaml.dump({
            "options": {
                "port": {"type": "int", "default": 5432, "description": "Listen port"},
                "ca_country_name": {"type": "string", "default": "", "description": "Country"},
                "ca_locality": {"type": "string", "default": "", "description": "Locality"},
                "max_ttl": {"type": "string", "default": "720h", "description": "Max lease TTL"},
            },
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": config_yaml, "actions_yaml": ""},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        # Non-empty defaults shown.
        assert "`5432`" in content
        assert "`720h`" in content
        # Empty-string defaults are schema noise: omitted, with a marker.
        assert "`ca_country_name`" not in content
        assert "`ca_locality`" not in content
        assert "2 options with empty defaults" in content

    def test_charm_config_empty_only_section(self, tmp_path):
        import yaml
        config_yaml = yaml.dump({
            "options": {
                "ca_country_name": {"type": "string", "default": "", "description": "Country"},
            },
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": config_yaml, "actions_yaml": ""},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "No non-empty option defaults" in content

    def test_charm_actions_listed(self, tmp_path):
        import yaml
        actions_yaml = yaml.dump({
            "initialize": {"description": "Initialize the service"},
            "unseal": {"description": "Unseal the service"},
            "get-status": {"description": "Get service status"},
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": actions_yaml},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "## Available actions" in content
        assert "`initialize`" in content
        assert "`unseal`" in content
        assert "`get-status`" in content
        # descriptions included so the model knows what each action does
        assert "Initialize the service" in content

    def test_charm_actions_omitted_when_empty(self, tmp_path):
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": ""},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Available actions" not in content

    def test_empty_actions_yaml_omits_section(self, tmp_path):
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": "%%% not yaml"},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Available actions" not in content

    def test_charm_links_listed(self, tmp_path):
        import yaml
        metadata_yaml = yaml.dump({
            "name": "openbao",
            "docs": "https://discourse.charmhub.io/t/openbao-operator-machine/12983",
            "source": ["https://github.com/canonical/openbao-omnicraft"],
            "issues": ["https://github.com/canonical/openbao-omnicraft/issues"],
            "website": ["https://canonical-openbao-charms.readthedocs-hosted.com/"],
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": "", "metadata_yaml": metadata_yaml},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "## Charm links" in content
        assert "https://discourse.charmhub.io/t/openbao-operator-machine/12983" in content
        assert "https://github.com/canonical/openbao-omnicraft" in content
        assert "https://github.com/canonical/openbao-omnicraft/issues" in content
        assert "https://canonical-openbao-charms.readthedocs-hosted.com/" in content

    def test_charm_links_omitted_when_empty(self, tmp_path):
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": "", "metadata_yaml": ""},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Charm links" not in content

    def test_non_url_metadata_fields_not_rendered_as_links(self, tmp_path):
        import yaml
        metadata_yaml = yaml.dump({
            "name": "openbao",
            "summary": "A tool for managing secrets",
            "docs": "https://discourse.charmhub.io/t/openbao-operator-machine/12983",
        })
        ctx = {
            **_EMPTY_CONTEXT,
            "charm_config": {"config_yaml": "", "actions_yaml": "", "metadata_yaml": metadata_yaml},
        }
        path = generate_report(INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, ctx, str(tmp_path))
        content = open(path).read()
        assert "Charm links" in content
        assert "https://discourse.charmhub.io/t/openbao-operator-machine/12983" in content
        assert "managing secrets" not in content


def _render(tmp_path, context):
    path = generate_report(
        INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN, context, str(tmp_path)
    )
    with open(path) as f:
        return f.read()


class TestSnapSection:
    def test_renders_failed_service_and_logs(self, tmp_path):
        context = {
            "snap": {
                "packages": ["charmed-postgresql 16.4 123 16/stable canonical -"],
                "services": [
                    "charmed-postgresql.patroni enabled failed -",
                    "vault.vaultd disabled inactive -",
                ],
                "failed_services": ["charmed-postgresql.patroni"],
                "failed_changes": ["42 Error today today Start service charmed-postgresql.patroni"],
                "logs": {"charmed-postgresql.patroni": ["ERROR could not start"]},
            }
        }
        report = _render(tmp_path, context)
        assert "## Snap packages" in report
        assert "## Snap services" in report
        assert "**Failed services:** `charmed-postgresql.patroni`" in report
        assert "## Failed snap changes" in report
        assert "## Snap logs: `charmed-postgresql.patroni` (failed)" in report
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


class TestDeclaredPorts:
    """Kubernetes plans check declaration, not liveness (TASKS 4.6)."""

    def test_declared_port_is_a_pass_with_distinct_wording(self, tmp_path):
        context = {"plan_results": {"network_ports": {"type": "plan", "items": [
            {"port": 5432, "protocol": "tcp", "status": "declared"},
            {"port": 6432, "protocol": "tcp", "status": "not declared"},
        ]}}}
        report = _render(tmp_path, context)
        assert "`5432/tcp` → declared ✓" in report
        assert "`6432/tcp` → not declared ✗" in report
        assert "listening" not in report

    def test_listening_still_renders(self, tmp_path):
        context = {"plan_results": {"network_ports": {"type": "plan", "items": [
            {"port": 5432, "protocol": "tcp", "status": "listening"},
        ]}}}
        report = _render(tmp_path, context)
        assert "`5432/tcp` → listening ✓" in report

    def test_broad_network_lines_still_render(self, tmp_path):
        """Regression guard: the plan branch must not swallow the broad one."""
        context = {"plan_results": {"network_ports": {
            "type": "broad",
            "lines": ["LISTEN 0 128 0.0.0.0:5432"],
        }}}
        report = _render(tmp_path, context)
        assert "## Network ports" in report
        assert "0.0.0.0:5432" in report


class TestPlanContainersSection:
    def test_renders_found_and_not_found(self, tmp_path):
        context = {"plan_results": {"plan_containers": {"type": "plan", "items": [
            {"name": "postgresql", "status": "found"},
            {"name": "pgbouncer", "status": "not found"},
        ]}}}
        report = _render(tmp_path, context)
        assert "## Plan containers" in report
        assert "`postgresql` → found ✓" in report
        assert "`pgbouncer` → not found ✗" in report

    def test_absent_when_no_plan(self, tmp_path):
        report = _render(tmp_path, {"plan_results": {}})
        assert "## Plan containers" not in report


class TestSecretRedaction:
    """Secrets must not reach the report or a prompt built from it (TASKS 4.9)."""

    _PASSWORD = "hunter2-correct-horse"
    _TOKEN = "abcdefghijklmnop"
    _SECRET_URI = "secret:abc123def456"
    _HEALTH = "supersecret-env-value"

    def _context(self):
        return {
            "unit_logs": [f"Authorization: Bearer {self._TOKEN}", "all good"],
            "juju_config": {
                "password": {"value": self._PASSWORD, "default": "",
                             "source": "user", "type": "string"},
                "port": {"value": "5432", "default": "5432",
                         "source": "default", "type": "string"},
                "api-token": {"value": self._SECRET_URI, "default": "",
                              "source": "user", "type": "secret"},
            },
            "plan_results": {"health_commands": {"type": "plan", "items": [{
                "command": "env", "timeout_seconds": 5, "returncode": 0,
                "stdout": f"password={self._HEALTH}", "stderr": "",
            }]}},
            "collected_at": "2026-10-06T00:00:00+00:00",
        }

    def test_sensitive_config_value_is_redacted_and_name_kept(self, tmp_path):
        report = _render(tmp_path, self._context())
        assert self._PASSWORD not in report
        assert "`password`" in report
        assert "[REDACTED]" in report

    def test_secret_typed_option_is_set_or_unset(self, tmp_path):
        report = _render(tmp_path, self._context())
        assert self._SECRET_URI not in report
        assert "abc123def456" not in report
        assert "`api-token`" in report
        assert "[REDACTED] (set)" in report

    def test_log_and_health_secrets_are_scrubbed(self, tmp_path):
        report = _render(tmp_path, self._context())
        assert self._TOKEN not in report
        assert self._HEALTH not in report
        assert "password=[REDACTED]" in report

    def test_ordinary_config_value_is_preserved(self, tmp_path):
        report = _render(tmp_path, self._context())
        assert "5432" in report

    def test_status_message_is_scrubbed(self, tmp_path):
        path = generate_report(
            INCIDENT_ID, "postgresql/0", "blocked", FIRST_SEEN,
            self._context(), str(tmp_path),
            status_message="auth failed with token=leaked-value",
        )
        with open(path) as f:
            report = f.read()
        assert "leaked-value" not in report
        assert "token=[REDACTED]" in report

    def test_prompt_built_from_report_has_no_secret(self, tmp_path):
        report = _render(tmp_path, self._context())
        prompt = build_suggest_prompt(report)
        for secret in (self._PASSWORD, self._TOKEN, self._SECRET_URI, self._HEALTH):
            assert secret not in prompt
