"""Unit tests for machine-charm co-located unit monitoring (controller API).

The principal path (`goal-state`) is covered by test_charm_status.py. These
tests cover the opt-in controller path added in 4.3: config parsing, the
credential prerequisite, host/exclusion filtering, and reporting other Jaime
units on the same machine.
"""

import unittest.mock as mock

from charm import JaimeCharm
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from jaime.principal import StatusTracker

_DEFAULT_CONFIG = {
    "watch-statuses": "error,blocked",
    "failure-timeout-minutes": 5,
    "cooldown-minutes": 30,
}

# Shaped from a real Client.FullStatus capture (see tests/unit/test_controller.py).
# ubuntu/0 is on machine 0 with two nested subordinates whose own `machine` is
# the empty string; postgresql/0 is on machine 1.
_MACHINE_STATUS = {
    "applications": {
        "ubuntu": {
            "units": {
                "ubuntu/0": {
                    "machine": "0",
                    "workload-status": {
                        "status": "active", "info": "",
                        "since": "2026-09-23T12:45:01Z",
                    },
                    "subordinates": {
                        "jaime/0": {
                            "machine": "",
                            "workload-status": {
                                "status": "active", "info": "Ready",
                                "since": "2026-09-23T12:54:20Z",
                            },
                        },
                        "logrotated/0": {
                            "machine": "",
                            "workload-status": {
                                "status": "blocked", "info": "broken",
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
                        "status": "active", "info": "",
                        "since": "2026-09-23T13:00:00Z",
                    },
                },
            },
        },
    },
}


def make_harness(tmp_path, config_overrides=None, with_principal=True):
    cfg = {**_DEFAULT_CONFIG, **(config_overrides or {})}
    with mock.patch.object(JaimeCharm, "_ensure_diagnostics"):
        h = Harness(JaimeCharm)
        h.update_config(cfg)
        if with_principal:
            h.add_relation("principal", "ubuntu")
        h.begin()
    h.charm._status_tracker = StatusTracker(state_path=str(tmp_path / "state.json"))
    return h


def _mock_client(status=_MACHINE_STATUS, login_error=None):
    client = mock.MagicMock()
    if login_error is not None:
        client.login.side_effect = login_error
    client.full_status.return_value = status
    cm = mock.MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm, client


class TestWatchApplications:
    def test_empty_by_default(self, tmp_path):
        h = make_harness(tmp_path)
        assert h.charm._watch_applications() == []

    def test_names_parsed_and_stripped(self, tmp_path):
        h = make_harness(tmp_path, {"watch-applications": " app1, app2 "})
        assert h.charm._watch_applications() == ["app1", "app2"]

    def test_star_preserved(self, tmp_path):
        h = make_harness(tmp_path, {"watch-applications": "*"})
        assert h.charm._watch_applications() == ["*"]


class TestPrerequisite:
    def test_default_needs_no_credentials(self, tmp_path):
        h = make_harness(tmp_path)
        assert h.charm._prerequisite_error() is None

    def test_missing_credentials_blocks_when_watching(self, tmp_path):
        h = make_harness(tmp_path, {"watch-applications": "logrotated"})
        err = h.charm._prerequisite_error()
        assert "juju-api-user and juju-api-password" in err

    def test_missing_agent_conf_is_transient(self, tmp_path):
        h = make_harness(
            tmp_path,
            {
                "watch-applications": "logrotated",
                "juju-api-user": "observer",
                "juju-api-password": "pw",
            },
        )
        with mock.patch("charm.agent_conf_path", return_value=None):
            assert h.charm._prerequisite_error() is None

    def test_rejected_credentials_block(self, tmp_path):
        from jaime.controller import ControllerAuthError

        h = make_harness(
            tmp_path,
            {
                "watch-applications": "logrotated",
                "juju-api-user": "observer",
                "juju-api-password": "pw",
            },
        )
        cm, _ = _mock_client(login_error=ControllerAuthError("nope"))
        with mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            err = h.charm._prerequisite_error()
        assert err == "juju-api credentials rejected by controller"


class TestCoLocatedFiltering:
    def _fetch(self, h, watch, machine="0", status=_MACHINE_STATUS):
        h.update_config({"watch-applications": watch})
        cm, _ = _mock_client(status=status)
        with mock.patch.object(JaimeCharm, "_machine_id", return_value=machine), \
             mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            return h.charm._fetch_co_located_statuses()

    def test_named_app_filtering_excludes_principal_and_self(self, tmp_path):
        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses, other = self._fetch(h, "logrotated")
        assert set(statuses) == {"logrotated/0"}
        assert statuses["logrotated/0"]["status"] == "blocked"
        # jaime/0 is this unit, so it is not "other".
        assert other == []

    def test_star_selects_every_co_located_unit(self, tmp_path):
        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses, _ = self._fetch(h, "*")
        # ubuntu/0 (principal) and jaime/0 (self) excluded; postgresql/0 is on
        # machine 1, so the host filter drops it.
        assert set(statuses) == {"logrotated/0"}

    def test_host_filter_drops_other_machines(self, tmp_path):
        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses, _ = self._fetch(h, "*", machine="1")
        assert set(statuses) == {"postgresql/0"}

    def test_named_app_not_on_this_host_yields_nothing(self, tmp_path):
        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses, _ = self._fetch(h, "postgresql")
        assert statuses == {}

    def test_unknown_machine_id_fails_closed(self, tmp_path):
        from jaime.controller import ControllerError

        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        cm, _ = _mock_client()
        with mock.patch.object(JaimeCharm, "_machine_id", return_value=None), \
             mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            with mock.patch("charm.extract_unit_statuses") as extract:
                import pytest
                with pytest.raises(ControllerError):
                    h.charm._fetch_co_located_statuses()
            # Never even reaches unit extraction, so no remote unit is reported.
            extract.assert_not_called()

    def test_other_jaime_units_detected(self, tmp_path):
        h = make_harness(tmp_path, {
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        # A second principal on the same machine with its own Jaime unit.
        # This unit is jaime/0, so jaime/1 is the "other".
        status = {
            "applications": {
                "ubuntu": {
                    "units": {
                        "ubuntu/0": {
                            "machine": "0",
                            "workload-status": {"status": "active", "info": ""},
                            "subordinates": {
                                "jaime/0": {
                                    "machine": "",
                                    "workload-status": {"status": "active", "info": "Ready"},
                                },
                            },
                        },
                    },
                },
                "other": {
                    "units": {
                        "other/0": {
                            "machine": "0",
                            "workload-status": {"status": "active", "info": ""},
                            "subordinates": {
                                "jaime/1": {
                                    "machine": "",
                                    "workload-status": {"status": "active", "info": "Ready"},
                                },
                            },
                        },
                    },
                },
            },
        }
        cm, _ = _mock_client(status=status)
        with mock.patch.object(JaimeCharm, "_machine_id", return_value="0"), \
             mock.patch.object(JaimeCharm, "_watch_applications", return_value=["*"]), \
             mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            statuses, other = h.charm._fetch_co_located_statuses()
        assert other == ["jaime/1"]
        # Both Jaime units are excluded from the monitored set; the second
        # principal is itself watched.
        assert set(statuses) == {"other/0"}


class TestUpdateStatus:
    def test_principal_always_watched_without_controller(self, tmp_path):
        h = make_harness(tmp_path)
        with mock.patch.object(JaimeCharm, "_log_principal_status") as log_principal, \
             mock.patch("charm.JujuControllerClient") as client:
            h.charm._on_update_status(mock.MagicMock())
        log_principal.assert_called_once()
        client.assert_not_called()

    def test_no_controller_connection_when_watch_empty(self, tmp_path):
        h = make_harness(tmp_path)
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses") as fetch:
            h.charm._on_update_status(mock.MagicMock())
        fetch.assert_not_called()

    def test_co_located_units_are_processed(self, tmp_path):
        h = make_harness(tmp_path, {
            "watch-applications": "logrotated",
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses = {
            "logrotated/0": {"status": "blocked", "since": "2026-09-23T18:17:01Z", "machine": "0"},
        }
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses",
                               return_value=(statuses, [])), \
             mock.patch.object(JaimeCharm, "_process_unit") as process:
            h.charm._on_update_status(mock.MagicMock())
        process.assert_called_once()
        assert process.call_args[0][0] == "logrotated/0"

    def test_missing_credentials_blocks_and_skips_fetch(self, tmp_path):
        h = make_harness(tmp_path, {"watch-applications": "logrotated"})
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses") as fetch:
            h.charm._on_update_status(mock.MagicMock())
        fetch.assert_not_called()
        assert isinstance(h.charm.unit.status, BlockedStatus)

    def test_other_jaime_units_reported_in_status(self, tmp_path):
        h = make_harness(tmp_path, {
            "watch-applications": "*",
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        h.charm.unit.status = ActiveStatus("Ready")
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses",
                               return_value=({}, ["jaime/0"])):
            h.charm._on_update_status(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "other Jaime unit" in h.charm.unit.status.message
        assert "jaime/0" in h.charm.unit.status.message
        # The monitored set is retained alongside the other-Jaime warning.
        assert "monitoring ubuntu" in h.charm.unit.status.message


class TestMonitoredApplications:
    """4.7: the active status names the applications actually being watched."""

    def test_ready_message_names_principal(self, tmp_path):
        h = make_harness(tmp_path)
        assert h.charm._ready_message() == "Ready: monitoring ubuntu"

    def test_ready_message_plain_without_principal(self, tmp_path):
        h = make_harness(tmp_path, with_principal=False)
        assert h.charm._ready_message() == "Ready"

    def test_principal_seeded_when_watch_empty(self, tmp_path):
        """The principal is always monitored, so it is always named."""
        h = make_harness(tmp_path)
        with mock.patch.object(JaimeCharm, "_log_principal_status"):
            h.charm._on_update_status(mock.MagicMock())
        assert h.charm._monitored_applications() == ["ubuntu"]

    def test_co_located_apps_added_to_monitored_set(self, tmp_path):
        h = make_harness(tmp_path, {
            "watch-applications": "logrotated",
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        statuses = {"logrotated/0": {"status": "active", "since": "2026-01-01T00:00:00Z"}}
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_prerequisite_error", return_value=None), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses",
                               return_value=(statuses, [])):
            h.charm._on_update_status(mock.MagicMock())
        assert h.charm._monitored_applications() == ["ubuntu", "logrotated"]
        assert h.charm.unit.status.message == "Ready: monitoring ubuntu, logrotated"

    def test_unresolved_configured_app_absent(self, tmp_path):
        """A configured app with no co-located unit is not named."""
        h = make_harness(tmp_path, {
            "watch-applications": "ghost",
            "juju-api-user": "observer", "juju-api-password": "pw",
        })
        with mock.patch.object(JaimeCharm, "_log_principal_status"), \
             mock.patch.object(JaimeCharm, "_prerequisite_error", return_value=None), \
             mock.patch.object(JaimeCharm, "_fetch_co_located_statuses",
                               return_value=({}, [])):
            h.charm._on_update_status(mock.MagicMock())
        assert h.charm._monitored_applications() == ["ubuntu"]

    def test_set_derived_from_tracker_and_config(self, tmp_path):
        """Derived from persisted observations, so it survives config-changed."""
        h = make_harness(tmp_path, {"watch-applications": "ubuntu,logrotated"})
        h.charm._status_tracker._state = {"ubuntu/0": {}, "logrotated/0": {}}
        assert h.charm._monitored_applications() == ["ubuntu", "logrotated"]
        h.update_config({"watch-applications": ""})
        assert h.charm._monitored_applications() == ["ubuntu"]

    def test_wildcard_includes_every_tracked_app(self, tmp_path):
        h = make_harness(tmp_path, {"watch-applications": "*"})
        h.charm._status_tracker._state = {"ubuntu/0": {}, "logrotated/0": {}}
        assert h.charm._monitored_applications() == ["ubuntu", "logrotated"]


class TestPrincipalStatusMessages:
    """Enrichment of the goal-state principal path with Juju's status message."""

    def _messages(self, h, status=None):
        cm, _ = _mock_client(status=status or _MACHINE_STATUS)
        with mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            return h.charm._principal_status_messages()

    def test_empty_message_omitted(self, tmp_path):
        """Active units with an empty message yield no entry (caller defaults to '')."""
        h = make_harness(
            tmp_path, with_principal=True,
            config_overrides={"juju-api-user": "observer", "juju-api-password": "pw"},
        )
        messages = self._messages(h)
        assert messages == {}

    def test_returns_status_reason_messages(self, tmp_path):
        status = {
            "applications": {
                "ubuntu": {
                    "units": {
                        "ubuntu/0": {
                            "machine": "0",
                            "workload-status": {
                                "status": "blocked",
                                "info": "Please initialize OpenBao or integrate with an auto-unseal provider",
                                "since": "2026-09-23T18:17:01Z",
                            },
                        },
                    },
                },
            },
        }
        h = make_harness(
            tmp_path, with_principal=True,
            config_overrides={"juju-api-user": "observer", "juju-api-password": "pw"},
        )
        messages = self._messages(h, status=status)
        assert messages == {
            "ubuntu/0": "Please initialize OpenBao or integrate with an auto-unseal provider"
        }

    def test_empty_without_credentials(self, tmp_path):
        h = make_harness(tmp_path, with_principal=True)
        assert h.charm._principal_status_messages() == {}

    def test_empty_when_controller_unreachable(self, tmp_path):
        from jaime.controller import ControllerError

        h = make_harness(
            tmp_path, with_principal=True,
            config_overrides={"juju-api-user": "observer", "juju-api-password": "pw"},
        )
        cm, _ = _mock_client(status=None)
        cm.full_status.side_effect = ControllerError("boom")
        with mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm):
            assert h.charm._principal_status_messages() == {}

    def test_message_reaches_process_unit(self, tmp_path):
        """The principal status message is passed through to the lifecycle."""
        import datetime

        status = {
            "applications": {
                "ubuntu": {
                    "units": {
                        "ubuntu/0": {
                            "machine": "0",
                            "workload-status": {
                                "status": "blocked",
                                "info": "Please initialize OpenBao or integrate with an auto-unseal provider",
                                "since": "2026-09-23T18:17:01Z",
                            },
                        },
                    },
                },
            },
        }
        h = make_harness(
            tmp_path, with_principal=True,
            config_overrides={"juju-api-user": "observer", "juju-api-password": "pw"},
        )
        cm, _ = _mock_client(status=status)
        gs = mock.MagicMock()
        unit = mock.MagicMock()
        unit.status = "blocked"
        unit.since = datetime.datetime(2026, 9, 23, 18, 17, 1, tzinfo=datetime.timezone.utc)
        gs.relations = {"principal": {"ubuntu/0": unit}}
        with mock.patch("charm.agent_conf_path", return_value="/tmp/agent.conf"), \
             mock.patch("charm.parse_agent_conf", return_value={
                 "api_address": "10.0.0.1:17070", "ca_cert": "CERT", "model_uuid": "uuid",
             }), \
             mock.patch("charm.JujuControllerClient", return_value=cm), \
             mock.patch("charm.goal_state", return_value=gs), \
             mock.patch.object(JaimeCharm, "_process_unit") as process:
            h.charm._log_principal_status()
        assert process.called
        args = process.call_args[0]
        assert args[0] == "ubuntu/0"
        assert args[1] == "blocked"
        assert args[3] == "Please initialize OpenBao or integrate with an auto-unseal provider"
