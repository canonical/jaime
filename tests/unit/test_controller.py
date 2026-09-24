"""Unit tests for jaime.controller (Juju controller API client)."""

import json
import unittest.mock as mock

import pytest

from jaime.controller import (
    ControllerAuthError,
    ControllerError,
    JujuControllerClient,
    extract_unit_statuses,
    parse_agent_conf,
)

AGENT_CONF = """\
# format 2.0
tag: unit-jaime-k8s-0
cacert: |
  -----BEGIN CERTIFICATE-----
  FAKECERT
  -----END CERTIFICATE-----
apiaddresses:
- 10.0.0.1:17070
model: model-11111111-2222-3333-4444-555555555555
oldpassword: unitpassword
"""


class TestParseAgentConf:
    def test_parses_all_fields(self, tmp_path):
        path = tmp_path / "agent.conf"
        path.write_text(AGENT_CONF)
        conf = parse_agent_conf(str(path))
        assert conf["api_address"] == "10.0.0.1:17070"
        assert conf["model_uuid"] == "11111111-2222-3333-4444-555555555555"
        assert "BEGIN CERTIFICATE" in conf["ca_cert"]

    def test_strips_model_prefix(self, tmp_path):
        path = tmp_path / "agent.conf"
        path.write_text(AGENT_CONF)
        conf = parse_agent_conf(str(path))
        assert not conf["model_uuid"].startswith("model-")


class TestJujuControllerClient:
    def _make_client(self):
        return JujuControllerClient("10.0.0.1:17070", "FAKECERT", "model-uuid")

    def test_url_uses_bare_uuid(self):
        client = self._make_client()
        assert "/model/model-uuid/api" in client._url

    def test_login_sends_correct_envelope(self):
        client = self._make_client()
        client._ws = mock.MagicMock()
        client._ws.recv.return_value = json.dumps({
            "request-id": 1,
            "response": {
                "facades": [{"name": "Client", "versions": [5, 6]}],
                "user-info": {"identity": "user-jaime-observer"},
            },
        })
        result = client.login("jaime-observer", "secret")
        sent = json.loads(client._ws.send.call_args[0][0])
        assert result["user-info"]["identity"] == "user-jaime-observer"
        assert sent["type"] == "Admin"
        assert sent["request"] == "Login"
        assert sent["params"]["auth-tag"] == "user-jaime-observer"
        assert sent["params"]["credentials"] == "secret"
        assert client.facades["Client"] == [5, 6]

    def test_login_adds_user_prefix(self):
        client = self._make_client()
        client._ws = mock.MagicMock()
        client._ws.recv.return_value = json.dumps({"request-id": 1, "response": {}})
        client.login("observer", "pw")
        sent = json.loads(client._ws.send.call_args[0][0])
        assert sent["params"]["auth-tag"] == "user-observer"

    def test_login_auth_failure_raises_auth_error(self):
        client = self._make_client()
        client._ws = mock.MagicMock()
        client._ws.recv.return_value = json.dumps({
            "request-id": 1, "error": "invalid entity name or password",
        })
        with pytest.raises(ControllerAuthError):
            client.login("observer", "wrongpw")

    def test_full_status_calls_client_facade(self):
        client = self._make_client()
        client._ws = mock.MagicMock()
        client.facades = {"Client": [5, 6]}
        client._ws.recv.return_value = json.dumps({
            "request-id": 1, "response": {"applications": {}},
        })
        result = client.full_status()
        sent = json.loads(client._ws.send.call_args[0][0])
        assert sent["type"] == "Client"
        assert sent["request"] == "FullStatus"
        assert sent["version"] == 6  # highest available
        assert result == {"applications": {}}

    def test_error_response_raises_controller_error(self):
        client = self._make_client()
        client._ws = mock.MagicMock()
        client._ws.recv.return_value = json.dumps({
            "request-id": 1, "error": "permission denied", "error-code": "forbidden",
        })
        with pytest.raises(ControllerError, match="permission denied"):
            client.full_status()


_FULL_STATUS = {
    "applications": {
        "postgresql-k8s": {
            "status": {"status": "active"},
            "units": {
                "postgresql-k8s/0": {
                    "workload-status": {
                        "status": "blocked",
                        "info": "Unsatisfied plugin dependencies",
                        "since": "2026-08-18T20:35:00Z",
                    },
                    "agent-status": {"status": "idle"},
                },
            },
        },
        "mysql-k8s": {
            "units": {
                "mysql-k8s/0": {
                    "workload-status": {
                        "status": "active",
                        "info": "",
                        "since": "2026-08-18T20:00:00Z",
                    },
                },
            },
        },
        "jaime-k8s": {
            "units": {
                "jaime-k8s/0": {
                    "workload-status": {"status": "active"},
                },
            },
        },
    },
}


# A machine-model FullStatus, shaped from a real Client.FullStatus response
# captured against Juju 3.6 (homelab, manual cloud). Two things it records
# that the Kubernetes fixture above cannot exercise:
#
#   - subordinate units are nested under the principal unit's `subordinates`
#     map; their own application's `units` is empty
#   - a nested subordinate's `machine` is the empty string, so it must inherit
#     its principal's machine to be host-filterable
#
# Raw API shape: workload-status carries `status`/`info`/`since` (the CLI's
# `current`/`message` is a different, display-only serialisation).
_FULL_STATUS_MACHINE = {
    "machines": {
        "0": {"juju-status": {"current": "started"}, "dns-name": "10.0.0.1"},
        "1": {"juju-status": {"current": "started"}, "dns-name": "10.0.0.2"},
    },
    "applications": {
        "ubuntu": {
            "subordinate-to": [],
            "units": {
                "ubuntu/0": {
                    "machine": "0",
                    "workload-status": {
                        "status": "active",
                        "info": "",
                        "since": "2026-09-23T12:45:01Z",
                    },
                    "subordinates": {
                        "jaime/0": {
                            "machine": "",
                            "workload-status": {
                                "status": "active",
                                "info": "Ready",
                                "since": "2026-09-23T12:54:20Z",
                            },
                        },
                        "logrotated/0": {
                            "machine": "",
                            "workload-status": {
                                "status": "blocked",
                                "info": "log rotation config missing",
                                "since": "2026-09-23T18:17:01Z",
                            },
                        },
                    },
                },
            },
        },
        "postgresql": {
            "units": {
                "postgresql/0": {
                    "machine": "1",
                    "workload-status": {
                        "status": "active",
                        "info": "",
                        "since": "2026-09-23T13:00:00Z",
                    },
                },
            },
        },
        # A subordinate application reports no units of its own.
        "jaime": {"subordinate-to": ["ubuntu"], "units": {}},
    },
}


class TestExtractUnitStatuses:
    def test_extracts_all_units(self):
        result = extract_unit_statuses(_FULL_STATUS)
        assert len(result) == 3
        assert result["postgresql-k8s/0"]["status"] == "blocked"
        assert result["postgresql-k8s/0"]["message"] == "Unsatisfied plugin dependencies"

    def test_watch_filter(self):
        result = extract_unit_statuses(
            _FULL_STATUS, watch_applications=["postgresql-k8s"]
        )
        assert list(result.keys()) == ["postgresql-k8s/0"]

    def test_exclude_filter(self):
        result = extract_unit_statuses(
            _FULL_STATUS, exclude_applications=["jaime-k8s"]
        )
        assert "jaime-k8s/0" not in result
        assert len(result) == 2

    def test_missing_workload_status_defaults_unknown(self):
        result = extract_unit_statuses({"applications": {"app": {"units": {"app/0": {}}}}})
        assert result["app/0"]["status"] == "unknown"


class TestExtractMachineUnitStatuses:
    def test_walks_subordinate_units(self):
        result = extract_unit_statuses(_FULL_STATUS_MACHINE)
        assert set(result) == {"ubuntu/0", "jaime/0", "logrotated/0", "postgresql/0"}
        assert result["logrotated/0"]["status"] == "blocked"
        assert result["logrotated/0"]["message"] == "log rotation config missing"

    def test_subordinate_inherits_principal_machine(self):
        result = extract_unit_statuses(_FULL_STATUS_MACHINE)
        assert result["logrotated/0"]["machine"] == "0"
        assert result["jaime/0"]["machine"] == "0"

    def test_principal_machine_recorded(self):
        result = extract_unit_statuses(_FULL_STATUS_MACHINE)
        assert result["ubuntu/0"]["machine"] == "0"
        assert result["postgresql/0"]["machine"] == "1"

    def test_subordinate_watched_under_unwatched_principal(self):
        """A watched subordinate is reachable even when its principal is not."""
        result = extract_unit_statuses(
            _FULL_STATUS_MACHINE, watch_applications=["logrotated"]
        )
        assert list(result) == ["logrotated/0"]

    def test_watch_does_not_leak_subordinates_of_watched_principal(self):
        result = extract_unit_statuses(
            _FULL_STATUS_MACHINE, watch_applications=["ubuntu"]
        )
        assert list(result) == ["ubuntu/0"]

    def test_exclude_removes_nested_subordinate(self):
        result = extract_unit_statuses(
            _FULL_STATUS_MACHINE, exclude_applications=["logrotated"]
        )
        assert "logrotated/0" not in result
        assert "jaime/0" in result

    def test_excluded_principal_still_exposes_watched_subordinate(self):
        """Excluding a principal must not hide its co-located subordinates.

        The machine charm always reads the principal from goal-state, so it
        excludes the principal application from the controller set; its
        co-located subordinates must remain visible.
        """
        result = extract_unit_statuses(
            _FULL_STATUS_MACHINE, exclude_applications=["ubuntu"]
        )
        assert "ubuntu/0" not in result
        assert result["logrotated/0"]["machine"] == "0"

    def test_machine_defaults_empty_when_absent(self):
        result = extract_unit_statuses(_FULL_STATUS)
        assert result["postgresql-k8s/0"]["machine"] == ""
