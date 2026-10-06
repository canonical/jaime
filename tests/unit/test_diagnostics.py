import json
import os
import tempfile

from jaime.diagnostics import (
    DIAGNOSTICS_SCHEMA,
    K8S_DIAGNOSTICS_CAPS,
    K8S_MAX_LOG_PATTERN_CHARS,
    build_prompt,
    make_empty_plan,
    read_diagnostics_file,
    validate_diagnostics,
    validate_k8s_diagnostics,
    write_diagnostics_file,
)


class TestValidateDiagnostics:
    def test_valid_full_plan_returns_empty_errors(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [
                    {"path": "/var/log/test.log", "priority": "high", "description": "test log"}
                ],
                "processes": [
                    {"name": "testd", "expected_min_count": 1, "expected_max_count": 2}
                ],
                "env_variables": ["TEST_VAR"],
                "network": {"ports": [{"port": 8080, "protocol": "tcp"}]},
                "systemd_units": ["testd.service"],
                "health_commands": [{"command": "systemctl is-active testd", "timeout_seconds": 5}],
            },
        }
        errors = validate_diagnostics(plan)
        assert errors == []

    def test_not_a_dict_returns_error(self):
        errors = validate_diagnostics("not a dict")
        assert errors == ["diagnostics must be a JSON object"]

    def test_empty_dict_returns_errors(self):
        errors = validate_diagnostics({})
        assert "missing required field: 'principal_name'" in errors
        assert "missing required field: 'monitoring_plan'" in errors

    def test_missing_monitoring_plan_field(self):
        plan = {"principal_name": "test-app"}
        errors = validate_diagnostics(plan)
        assert any("missing required field" in e and "monitoring_plan" in e for e in errors)

    def test_invalid_monitoring_plan_type(self):
        plan = {"principal_name": "test-app", "monitoring_plan": "not an object"}
        errors = validate_diagnostics(plan)
        assert any("must be a JSON object" in e for e in errors)

    def test_log_files_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": "not a list",
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("log_files' must be a list" in e for e in errors)

    def test_log_file_missing_fields(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [{"path": "/var/log/test.log"}],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("missing 'priority'" in e for e in errors)
        assert any("missing 'description'" in e for e in errors)

    def test_log_file_invalid_priority(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [
                    {"path": "/var/log/test.log", "priority": "urgent", "description": "test"}
                ],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("priority" in e and "must be" in e for e in errors)

    def test_process_missing_name(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [{"expected_min_count": 1}],
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("missing 'name'" in e for e in errors)

    def test_processes_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": "not a list",
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("processes' must be a list" in e for e in errors)

    def test_env_variables_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": "not a list",
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("env_variables' must be a list" in e for e in errors)

    def test_env_variables_non_string(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [123],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("must be a string" in e for e in errors)

    def test_network_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": "not an object",
            },
        }
        errors = validate_diagnostics(plan)
        assert any("network' must be an object" in e for e in errors)

    def test_network_ports_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": "not a list"},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("ports' must be a list" in e for e in errors)

    def test_network_port_missing_fields(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": [{"port": 8080}]},
            },
        }
        errors = validate_diagnostics(plan)
        assert any("missing 'protocol'" in e for e in errors)

    def test_systemd_units_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
                "systemd_units": "not a list",
            },
        }
        errors = validate_diagnostics(plan)
        assert any("systemd_units' must be a list" in e for e in errors)

    def test_systemd_units_non_string(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
                "systemd_units": [123],
            },
        }
        errors = validate_diagnostics(plan)
        assert any("must be a string" in e for e in errors)

    def test_health_commands_bad_type(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
                "health_commands": "not a list",
            },
        }
        errors = validate_diagnostics(plan)
        assert any("health_commands' must be a list" in e for e in errors)

    def test_health_command_missing_command(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
                "health_commands": [{"timeout_seconds": 5}],
            },
        }
        errors = validate_diagnostics(plan)
        assert any("missing 'command'" in e for e in errors)

    def test_optional_fields_omitted_are_valid(self):
        plan = {
            "principal_name": "test-app",
            "monitoring_plan": {
                "log_files": [],
                "processes": [],
                "env_variables": [],
                "network": {"ports": []},
            },
        }
        errors = validate_diagnostics(plan)
        assert errors == []


class TestDiagnosticsSchema:
    def test_schema_has_expected_fields(self):
        assert DIAGNOSTICS_SCHEMA["type"] == "object"
        assert "principal_name" in DIAGNOSTICS_SCHEMA["properties"]
        assert "monitoring_plan" in DIAGNOSTICS_SCHEMA["properties"]
        assert "principal_name" in DIAGNOSTICS_SCHEMA["required"]
        assert "monitoring_plan" in DIAGNOSTICS_SCHEMA["required"]


class TestBuildPrompt:
    def test_contains_principal_name(self):
        prompt = build_prompt("postgresql")
        assert "postgresql" in prompt
        assert "monitoring plan" in prompt.lower()

    def test_contains_schema_json(self):
        prompt = build_prompt("test-app")
        assert "log_files" in prompt
        assert "systemd_units" in prompt
        assert "health_commands" in prompt


class TestMakeEmptyPlan:
    def test_returns_correct_structure(self):
        plan = make_empty_plan("test-app")
        assert plan["principal_name"] == "test-app"
        assert "generated_at" in plan
        assert "+00:00" in plan["generated_at"] or plan["generated_at"].endswith("Z")
        mp = plan["monitoring_plan"]
        assert mp["log_files"] == []
        assert mp["processes"] == []
        assert mp["env_variables"] == []
        assert mp["network"]["ports"] == []
        assert mp["systemd_units"] == []
        assert mp["health_commands"] == []

    def test_plan_passes_validation(self):
        plan = make_empty_plan("test-app")
        errors = validate_diagnostics(plan)
        assert errors == []


class TestWriteReadDiagnosticsFile:
    def test_write_and_read_roundtrip(self):
        plan = make_empty_plan("roundtrip-test")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            written = write_diagnostics_file(plan, path)
            assert written == path

            read_back = read_diagnostics_file(path)
            assert read_back["principal_name"] == "roundtrip-test"
            assert "monitoring_plan" in read_back
        finally:
            os.unlink(path)

    def test_creates_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "nested", "diagnostics.json")
            plan = make_empty_plan("dir-test")
            written = write_diagnostics_file(plan, path)
            assert os.path.exists(written)
            read_back = read_diagnostics_file(path)
            assert read_back["principal_name"] == "dir-test"

    def test_accepts_json_string(self):
        plan = make_empty_plan("string-test")
        json_str = json.dumps(plan)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            write_diagnostics_file(json_str, path)
            read_back = read_diagnostics_file(path)
            assert read_back["principal_name"] == "string-test"
        finally:
            os.unlink(path)

    def test_read_missing_file_returns_none(self):
        result = read_diagnostics_file("/tmp/nonexistent-file-12345.json")
        assert result is None

    def test_read_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            f.write("not valid json")
            path = f.name
        try:
            result = read_diagnostics_file(path)
            assert result is None
        finally:
            os.unlink(path)


class TestValidateK8sDiagnostics:
    """The keyed-by-application Kubernetes plan (TASKS 4.6)."""

    def test_valid_full_plan_returns_empty_errors(self):
        plan = {
            "postgresql-k8s": {
                "containers": ["postgresql"],
                "log_patterns": ["FATAL", "out of memory"],
                "env_variables": ["PGDATA"],
                "ports": [{"port": 5432, "protocol": "tcp"}],
            },
        }
        assert validate_k8s_diagnostics(plan) == []

    def test_empty_plan_is_valid(self):
        assert validate_k8s_diagnostics({}) == []
        assert validate_k8s_diagnostics({"app": {}}) == []

    def test_not_an_object_returns_error(self):
        assert validate_k8s_diagnostics("nope") == ["diagnostics must be a JSON object"]
        assert validate_k8s_diagnostics([]) == ["diagnostics must be a JSON object"]

    def test_application_value_must_be_an_object(self):
        errors = validate_k8s_diagnostics({"app": "nope"})
        assert any("'app' must be a JSON object" in e for e in errors)

    def test_unknown_keys_are_rejected(self):
        errors = validate_k8s_diagnostics({"app": {"container": ["x"]}})
        assert any("unknown keys" in e and "container" in e for e in errors)

    def test_string_list_wrong_type(self):
        assert any(
            "must be a list" in e
            for e in validate_k8s_diagnostics({"app": {"containers": "nope"}})
        )

    def test_string_list_items_must_be_non_empty_strings(self):
        errors = validate_k8s_diagnostics({"app": {"env_variables": ["OK", "", 7]}})
        assert any("env_variables[1]" in e for e in errors)
        assert any("env_variables[2]" in e for e in errors)

    def test_over_cap_is_an_error(self):
        errors = validate_k8s_diagnostics(
            {"app": {"containers": [f"c{i}" for i in range(K8S_DIAGNOSTICS_CAPS["containers"] + 1)]}}
        )
        assert any("containers" in e and "max" in e for e in errors)

    def test_too_many_applications(self):
        plan = {f"app{i}": {} for i in range(K8S_DIAGNOSTICS_CAPS["applications"] + 1)}
        assert any("too many applications" in e for e in validate_k8s_diagnostics(plan))

    def test_bad_regex_is_an_error(self):
        errors = validate_k8s_diagnostics({"app": {"log_patterns": ["(unclosed"]}})
        assert any("not a valid regex" in e for e in errors)

    def test_log_pattern_too_long(self):
        errors = validate_k8s_diagnostics(
            {"app": {"log_patterns": ["x" * (K8S_MAX_LOG_PATTERN_CHARS + 1)]}}
        )
        assert any("exceeds" in e for e in errors)

    def test_port_must_be_an_integer(self):
        errors = validate_k8s_diagnostics({"app": {"ports": [{"port": "5432"}]}})
        assert any("port' must be an integer" in e for e in errors)

    def test_port_protocol_is_restricted(self):
        errors = validate_k8s_diagnostics(
            {"app": {"ports": [{"port": 5432, "protocol": "sctp"}]}}
        )
        assert any("protocol' must be 'tcp' or 'udp'" in e for e in errors)
