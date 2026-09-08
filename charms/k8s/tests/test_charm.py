"""Unit tests for JaimeK8sCharm substrate-specific behaviour.

Shared CoreMixin behaviour (config validation, incident status preservation,
provider resolution, usage summary) is tested once in test_core.py. This file
covers only what is specific to the k8s charm.
"""

import contextlib
import unittest.mock as mock

from charm import JaimeK8sCharm
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.testing import Harness


def _make_harness(config_overrides=None):
    h = Harness(JaimeK8sCharm)
    h.begin()
    if config_overrides:
        h.update_config(config_overrides)
    return h


class TestWatchApplications:
    def test_empty_watch_blocks_when_credentials_missing(self):
        """Empty watch-applications is opt-out, but connectivity comes first."""
        h = _make_harness({"watch-applications": ""})
        with mock.patch.object(h.charm, "_fetch_unit_statuses") as mock_fetch:
            h.charm._monitor()
        mock_fetch.assert_not_called()
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "juju-api-user" in h.charm.unit.status.message

    def test_empty_watch_active_when_prereqs_ok(self):
        """With prerequisites verified, empty watch-applications is the opt-out."""
        h = _make_harness({"watch-applications": ""})
        with mock.patch.object(h.charm, "_prerequisite_error", return_value=None), \
             mock.patch.object(h.charm, "_fetch_unit_statuses") as mock_fetch:
            h.charm._monitor()
        mock_fetch.assert_not_called()
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "watch-applications" in h.charm.unit.status.message

    def test_watch_applications_parsing(self):
        h = _make_harness({"watch-applications": " postgresql-k8s, mysql-k8s "})
        assert h.charm._watch_applications() == ["postgresql-k8s", "mysql-k8s"]

    def test_watch_applications_empty_parsing(self):
        h = _make_harness({"watch-applications": ""})
        assert h.charm._watch_applications() == []

    def test_with_apps_selected_blocks_until_credentials(self):
        """Watch-applications set but no observer credentials: block, no fetch."""
        h = _make_harness({"watch-applications": "postgresql-k8s"})
        with mock.patch.object(h.charm, "_fetch_unit_statuses") as mock_fetch:
            h.charm._monitor()
        mock_fetch.assert_not_called()
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "juju-api-user" in h.charm.unit.status.message

    def test_with_credentials_and_ok_prereqs_calls_fetch(self):
        h = _make_harness({
            "watch-applications": "postgresql-k8s",
            "juju-api-user": "jaime-observer",
            "juju-api-password": "secret",
        })
        with mock.patch.object(h.charm, "_prerequisite_error", return_value=None), \
             mock.patch.object(h.charm, "_fetch_unit_statuses", return_value={}):
            h.charm._monitor()
        # fetch called; empty result -> maintenance status
        assert isinstance(h.charm.unit.status, MaintenanceStatus)


class TestPrerequisiteCheck:
    """TASKS 4.2: block until the controller and Kubernetes API are verified."""

    def _controller_client_for(self, applications, login_error=None):
        client = mock.Mock()
        client.full_status.return_value = {"applications": applications}
        if login_error:
            client.login.side_effect = login_error
        return client

    def _patch_env(self, client=None, k8s_check=None, login_error=None):
        """Patch agent.conf, the controller client, and the k8s API client."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch(
            "charm.agent_conf_path", return_value="/tmp/agent.conf"
        ))
        stack.enter_context(mock.patch(
            "charm.parse_agent_conf",
            return_value={"api_address": "a", "ca_cert": "c", "model_uuid": "m"},
        ))

        controller = mock.MagicMock()
        controller.__enter__.return_value = (
            client or self._controller_client_for({}, login_error=login_error)
        )
        stack.enter_context(mock.patch(
            "charm.JujuControllerClient", return_value=controller
        ))
        if k8s_check is not None:
            k8s = mock.Mock()
            k8s.check_access.return_value = k8s_check
            stack.enter_context(mock.patch("charm.K8sApiClient", return_value=k8s))
        return stack

    def _harness_with_creds(self, watch="postgresql-k8s"):
        return _make_harness({
            "watch-applications": watch,
            "juju-api-user": "jaime-observer",
            "juju-api-password": "secret",
        })

    def test_missing_app_blocks(self):
        h = self._harness_with_creds("postgresql-k8s,mysql-k8s")
        client = self._controller_client_for(
            {"postgresql-k8s": {"units": {}}}
        )
        with self._patch_env(client=client, k8s_check=("ok", None)):
            msg = h.charm._prerequisite_error()
        assert "mysql-k8s" in msg
        assert "watch-applications not found" in msg

    def test_all_existing_and_ok_returns_none(self):
        h = self._harness_with_creds()
        client = self._controller_client_for({"postgresql-k8s": {"units": {}}})
        with self._patch_env(client=client, k8s_check=("ok", None)):
            assert h.charm._prerequisite_error() is None

    def test_empty_watch_still_checks_credentials(self):
        """Empty watch-applications is not a pass: connectivity is checked first."""
        h = _make_harness()
        msg = h.charm._prerequisite_error()
        assert "juju-api-user" in msg

    def test_empty_watch_passes_when_verified(self):
        """Empty watch-applications returns None once connectivity is verified."""
        h = self._harness_with_creds(watch="")
        client = self._controller_client_for({})
        with self._patch_env(client=client, k8s_check=("ok", None)):
            assert h.charm._prerequisite_error() is None

    def test_missing_credentials_blocks(self):
        h = _make_harness({"watch-applications": "postgresql-k8s"})
        msg = h.charm._prerequisite_error()
        assert "juju-api-user" in msg
        assert "juju-api-password" in msg

    def test_rejected_credentials_blocks(self):
        from jaime.controller import ControllerAuthError

        h = self._harness_with_creds()
        with self._patch_env(login_error=ControllerAuthError("bad creds")):
            msg = h.charm._prerequisite_error()
        assert "credentials rejected" in msg

    def test_transient_controller_error_does_not_block(self):
        from jaime.controller import ControllerError

        h = self._harness_with_creds()
        with self._patch_env(k8s_check=None) as stack:
            stack.enter_context(mock.patch(
                "charm.JujuControllerClient",
                side_effect=ControllerError("controller down"),
            ))
            assert h.charm._prerequisite_error() is None

    def test_missing_rbac_blocks(self):
        h = self._harness_with_creds()
        client = self._controller_client_for({"postgresql-k8s": {"units": {}}})
        with self._patch_env(
            client=client, k8s_check=("forbidden", "cannot read pod logs")
        ):
            msg = h.charm._prerequisite_error()
        assert "Kubernetes RBAC missing" in msg
        assert "cannot read pod logs" in msg

    def test_unreachable_k8s_does_not_block(self):
        h = self._harness_with_creds()
        client = self._controller_client_for({"postgresql-k8s": {"units": {}}})
        with self._patch_env(
            client=client, k8s_check=("unreachable", "cannot reach API")
        ):
            assert h.charm._prerequisite_error() is None

    def test_config_change_blocks_on_prereq(self):
        h = _make_harness({"watch-applications": "ghost-app"})
        with mock.patch.object(h.charm, "_prerequisite_error",
                               return_value="watch-applications not found on model: ghost-app"):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "ghost-app" in h.charm.unit.status.message

    def test_config_change_stays_active_when_all_good(self):
        h = _make_harness()
        with mock.patch.object(h.charm, "_prerequisite_error", return_value=None):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)

    def test_config_change_preserves_existing_block(self):
        """An invalid mode must not be overwritten by the prerequisite check."""
        h = _make_harness({"mode": "nonsense"})
        with mock.patch.object(h.charm, "_prerequisite_error",
                               return_value="watch-applications: x"):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "invalid mode" in h.charm.unit.status.message

    def test_monitor_blocks_on_prereq(self):
        h = _make_harness({"watch-applications": "postgresql-k8s"})
        with mock.patch.object(h.charm, "_prerequisite_error",
                               return_value="k8s prereq failed"), \
             mock.patch.object(h.charm, "_fetch_unit_statuses") as mock_fetch:
            h.charm._monitor()
        mock_fetch.assert_not_called()
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "k8s prereq failed" in h.charm.unit.status.message


class TestShowSetupSteps:
    def test_guide_reads_model_name(self):
        """The guide pre-fills the real model name instead of a placeholder."""
        h = Harness(JaimeK8sCharm)
        h.set_model_name("cool-model")
        h.begin()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "MODEL_NAME=cool-model" in result

    def test_guide_generates_password(self):
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "NEW_PASS=$(openssl rand -hex 16)" in result

    def test_guide_covers_rbac(self):
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "kubectl apply -f" in result
        assert "jaime-k8s-rbac.yaml" in result
        assert "MODEL_NAME" in result

    def test_guide_covers_juju_user_and_secrets(self):
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "juju add-user jaime-observer" in result
        assert "juju grant jaime-observer read ${MODEL_NAME}" in result
        assert "juju add-secret jaime-juju-api" in result
        assert "juju grant-secret jaime-juju-api jaime-k8s" in result
        assert "juju config jaime-k8s juju-api-user=jaime-observer" in result

    def test_guide_covers_watch_applications(self):
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "watch-applications=postgresql-k8s,mysql-k8s" in result

    def test_guide_covers_ai_token_grants_application(self):
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "juju add-secret jaime-token" in result
        # grant-secret takes the application, not the model.
        assert "juju grant-secret jaime-token jaime-k8s" in result
        assert "mode=suggest provider=openrouter" in result

    def test_guide_uses_charm_app_name(self):
        """The guide targets the charm's own application name, not a hardcoded one."""
        h = _make_harness()
        event = mock.MagicMock()
        event.params = {}
        h.charm._on_action_show_setup_steps(event)
        result = event.set_results.call_args[0][0]["result"]
        assert "jaime-k8s" in result
        assert "juju config jaime-k8s watch-applications" in result
