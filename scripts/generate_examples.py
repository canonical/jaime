#!/usr/bin/env python3
"""Regenerate the example plan and report from the real code.

`examples/diagnostics.json` and `examples/report.md` are committed so readers
can see the shape of Jaime's output without deploying it. Hand-maintaining them
means they drift from `report.py` every time a section changes, which they had.

This script builds one fixed incident (a machine charm whose principal is
blocked by a failed snap) and writes it out. `tests/unit/test_examples.py`
regenerates the same bytes and fails if the committed files differ, so the
examples cannot drift again.

Run `make examples` to refresh them.
"""

import json
import pathlib
import sys
import tempfile
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "jaime-package"))

from jaime import report  # noqa: E402
from jaime.diagnostics import validate_diagnostics  # noqa: E402

EXAMPLES_DIR = REPO_ROOT / "examples"

INCIDENT_ID = "e7a3f1b2-8c4d-4a5e-9b6f-2c1d3e4f5a6b"
UNIT_NAME = "postgresql/0"
WORKLOAD = "blocked"
FIRST_SEEN = "2026-07-14T09:55:00+00:00"
GENERATED_AT = "2026-07-14T10:05:00+00:00"

# The monitoring plan. Validated against the real schema before it is written.
PLAN = {
    "principal_name": "postgresql",
    "generated_at": GENERATED_AT,
    "monitoring_plan": {
        "log_files": [
            {
                "path": "/var/log/postgresql/postgresql-16-main.log",
                "priority": "high",
                "description": "Main PostgreSQL log",
            },
            {
                "path": "/var/log/postgresql/postgresql-16-main.log.1",
                "priority": "medium",
                "description": "Rotated PostgreSQL log (recent)",
            },
            {
                "path": "/var/log/postgresql/pg_stat_statements.log",
                "priority": "low",
                "description": "Query statistics log",
            },
        ],
        "processes": [
            {"name": "postgres", "expected_min_count": 1, "expected_max_count": 4, "parent": "postgres"},
            {"name": "pgbouncer", "expected_min_count": 1, "expected_max_count": 1},
        ],
        "systemd_units": [
            "postgresql@16-main.service",
            "postgresql-exporter.service",
        ],
        "env_variables": ["PGDATA", "PGPORT", "POSTGRES_PASSWORD"],
        "network": {"ports": [{"port": 5432, "protocol": "tcp"}, {"port": 6432, "protocol": "tcp"}]},
        "health_commands": [
            {"command": "systemctl is-active postgresql@16-main.service", "timeout_seconds": 10},
            {"command": "pg_isready -h localhost -p 5432", "timeout_seconds": 10},
        ],
    },
}

# One incident that exercises every machine section. Add a section here when a
# new one is added to report.py, or the example will silently omit it.
CONTEXT = {
    "collected_at": GENERATED_AT,
    "tracing_events": [],
    "unit_logs": [
        "2026-07-14 09:55:01 WARNING unit.postgresql/0.juju-log Cannot start: no replicas",
        "2026-07-14 09:56:10 ERROR unit.postgresql/0.juju-log snap service charmed-postgresql.patroni is failed",
        "2026-07-14 09:57:00 ERROR unit.postgresql/0.juju-log Hook start failed: disk full",
        "2026-07-14 09:58:00 INFO juju.worker Running cleanup hooks",
    ],
    "systemd_failed": ["postgresql@16-main.service"],
    "systemd_failed_detail": [
        {"unit": "postgresql@16-main.service", "status": "failed", "substate": "dead",
         "restarts": "3", "exec_main_status": "1"},
    ],
    "disk_usage": [
        "Filesystem     Size  Used Avail Use% Mounted on",
        "/dev/sda1       20G   19G  1.0G  95% /",
    ],
    "memory_summary": [
        "              total  used  free  shared  buff/cache  available",
        "Mem:           7.7G  6.1G  0.3G   0.1G       1.3G       1.2G",
        "Swap:          2.0G  0.5G  1.5G",
    ],
    "ss_connections": [
        "tcp LISTEN 0 128 0.0.0.0:6432 0.0.0.0:* users:((\"pgbouncer\",pid=1234,fd=7))",
        "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=800,fd=3))",
    ],
    "firewall_rules": {
        "iptables": [
            "Chain INPUT (policy DROP)",
            "target     prot opt source               destination",
            "ACCEPT     tcp  --  0.0.0.0/0            0.0.0.0/0            tcp dpt:6432",
        ],
        "ufw": ["Status: active", "6432/tcp                   ALLOW       Anywhere"],
    },
    "charm_config": {
        "config_yaml": (
            "options:\n"
            "  max_connections:\n"
            "    default: 100\n"
            "  port:\n"
            "    default: 5432\n"
        ),
    },
    "snap": {
        "packages": [
            "Name                  Version   Rev    Tracking       Publisher   Notes",
            "charmed-postgresql    16.4      123    16/stable      canonical✓  -",
            "core22                20240920  1622   latest/stable  canonical✓  base",
        ],
        "services": [
            "Name                             Startup   Current   Notes",
            "charmed-postgresql.patroni       enabled   failed    -",
            "charmed-postgresql.pgbackrest    enabled   active    -",
        ],
        "failed_changes": [
            "42  Error  today at 09:56  today at 09:56  Start service charmed-postgresql.patroni",
        ],
        "logs": {
            "charmed-postgresql": [
                "09:55:58 patroni[4321]: INFO: no action. I am (postgresql-0), the leader",
                "09:56:04 patroni[4321]: ERROR: could not connect to local PostgreSQL",
                "09:56:04 patroni[4321]: ERROR: Exiting",
                "09:56:05 systemd[1]: charmed-postgresql.patroni.service: Failed with result 'exit-code'",
            ],
        },
    },
    "plan_results": {
        "log_files": {
            "type": "plan",
            "items": [
                {
                    "path": "/var/log/postgresql/postgresql-16-main.log",
                    "priority": "high",
                    "description": "Main PostgreSQL log",
                    "status": "available",
                    "lines": [
                        "2026-07-14 09:55:01 UTC [12345] LOG:  received SIGHUP, reloading configuration files",
                        "2026-07-14 09:56:00 UTC [12346] WARNING:  database \"app\" has no active replicas",
                        "2026-07-14 09:57:30 UTC [12347] ERROR:  out of disk space on primary",
                    ],
                },
                {
                    "path": "/var/log/postgresql/postgresql-16-main.log.1",
                    "priority": "medium",
                    "description": "Rotated PostgreSQL log (recent)",
                    "status": "available",
                    "lines": [
                        "2026-07-14 09:50:00 UTC [12340] LOG:  checkpoint starting: time",
                        "2026-07-14 09:52:00 UTC [12341] LOG:  checkpoint complete: wrote 42 buffers",
                    ],
                },
                {
                    "path": "/var/log/postgresql/pg_stat_statements.log",
                    "priority": "low",
                    "description": "Query statistics log",
                    "status": "not_found",
                    "lines": [],
                },
            ],
        },
        "processes": {
            "type": "plan",
            "items": [
                {"name": "postgres", "expected_min_count": 1, "expected_max_count": 4,
                 "running_count": 0, "status": "too_few"},
                {"name": "pgbouncer", "expected_min_count": 1, "expected_max_count": 1,
                 "running_count": 1, "status": "ok"},
            ],
        },
        "systemd_units": {
            "type": "plan",
            "items": [
                {"unit": "postgresql@16-main.service", "status": "failed", "substate": "dead",
                 "restarts": "3", "exec_main_status": "1"},
                {"unit": "postgresql-exporter.service", "status": "active", "substate": "running",
                 "restarts": "0", "exec_main_status": "0"},
            ],
        },
        "network_ports": {
            "type": "plan",
            "items": [
                {"port": 5432, "protocol": "tcp", "status": "not_listening"},
                {"port": 6432, "protocol": "tcp", "status": "listening"},
            ],
        },
        "env_variables": {
            "type": "plan",
            "items": [
                {"name": "PGDATA", "status": "set"},
                {"name": "PGPORT", "status": "set"},
                {"name": "POSTGRES_PASSWORD", "status": "unset"},
            ],
        },
        "health_commands": {
            "type": "plan",
            "items": [
                {"command": "systemctl is-active postgresql@16-main.service", "timeout_seconds": 10,
                 "returncode": 1, "stdout": "", "stderr": "inactive"},
                {"command": "pg_isready -h localhost -p 5432", "timeout_seconds": 10,
                 "returncode": 2, "stdout": "", "stderr": "/tmp:5432 - no response"},
            ],
        },
    },
}


def build_diagnostics() -> str:
    """Return the example plan as JSON text, after checking it against the schema."""
    errors = validate_diagnostics(PLAN)
    if errors:
        raise SystemExit("example plan is invalid: " + "; ".join(errors))
    return json.dumps(PLAN, indent=2) + "\n"


def build_report() -> str:
    """Return the example report by running the real generator on a pinned clock."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(report, "datetime") as fake_datetime:
            fake_datetime.datetime.now.return_value.isoformat.return_value = GENERATED_AT
            path = report.generate_report(
                INCIDENT_ID, UNIT_NAME, WORKLOAD, FIRST_SEEN, CONTEXT, tmp
            )
        return pathlib.Path(path).read_text()


def main() -> None:
    EXAMPLES_DIR.mkdir(exist_ok=True)
    (EXAMPLES_DIR / "diagnostics.json").write_text(build_diagnostics())
    (EXAMPLES_DIR / "report.md").write_text(build_report())
    print(f"Regenerated {EXAMPLES_DIR}/diagnostics.json and report.md")


if __name__ == "__main__":
    main()
