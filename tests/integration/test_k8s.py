"""Integration tests for the Jaime Kubernetes standalone charm.

Covers TASKS.md 5.2: the k8s deployment path, plus the two documented
failure modes — missing Kubernetes RBAC and rejected controller credentials.

Requires a bootstrapped Kubernetes controller (MicroK8s).
"""

import pathlib
import subprocess

import jubilant
import pytest

from .conftest import FAILURE_TIMEOUT_MINUTES, K8S_APP_NAME

pytestmark = pytest.mark.integration

WATCHED_APP = "postgresql-k8s"
WATCHED_CHANNEL = "14/stable"

JUJU_API_USER = "jaime-observer"
JUJU_API_PASSWORD = "integration-test-password"

_RBAC_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "charms"
    / "k8s"
    / "jaime-k8s-rbac.yaml"
)


def _jaime_unit() -> str:
    return f"{K8S_APP_NAME}/0"


def _apply_rbac(juju, remove=False) -> None:
    """Apply (or remove) the jaime-k8s Role/RoleBinding in the model namespace."""
    cmd = ["kubectl", "delete" if remove else "apply", "-f", str(_RBAC_PATH)]
    if not remove:
        cmd += ["-n", juju.model]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def observer_credentials(juju):
    """Create a Juju user with read access to the model under test."""
    juju.cli("add-user", JUJU_API_USER, include_model=False)
    juju.cli(
        "change-user-password", JUJU_API_USER,
        include_model=False,
        stdin=f"{JUJU_API_PASSWORD}\n{JUJU_API_PASSWORD}\n",
    )
    juju.cli("grant", JUJU_API_USER, "read", juju.model, include_model=False)
    yield JUJU_API_USER, JUJU_API_PASSWORD
    juju.cli("remove-user", JUJU_API_USER, "--yes", include_model=False)


@pytest.fixture(scope="module")
def deployed_k8s(juju, k8s_charm, observer_credentials, ai_provider, ai_token):
    """Deploy the k8s charm under its required application name.

    The RoleBinding in charms/k8s/jaime-k8s-rbac.yaml is bound to the
    jaime-k8s ServiceAccount, which Juju names after the application, so the
    name is not free to change. The RBAC is applied so the prerequisite check
    (TASKS 4.2) passes and the charm reaches active.
    """
    user, password = observer_credentials

    juju.deploy(WATCHED_APP, channel=WATCHED_CHANNEL, trust=True)

    config = {
        "mode": "suggest" if ai_token else "observe",
        "provider": ai_provider if ai_token else "none",
        "watch-applications": WATCHED_APP,
        "watch-statuses": "error,blocked",
        "failure-timeout-minutes": FAILURE_TIMEOUT_MINUTES,
        "cooldown-minutes": 1,
        "juju-api-user": user,
        "juju-api-password": password,
    }
    if ai_token:
        config["api-token"] = ai_token

    juju.deploy(k8s_charm, K8S_APP_NAME, config=config, trust=True)
    juju.wait(lambda s: jubilant.all_active(s, WATCHED_APP, K8S_APP_NAME))
    return juju


class TestK8sDeployment:
    def test_charm_reaches_active(self, deployed_k8s):
        status = deployed_k8s.status()
        assert status.apps[K8S_APP_NAME].app_status.current == "active"

    def test_charm_is_not_subordinate(self, deployed_k8s):
        """The k8s charm is standalone; it has no relation to watched apps."""
        status = deployed_k8s.status()
        assert not status.apps[K8S_APP_NAME].subordinate_to

    def test_show_status_action_succeeds(self, deployed_k8s):
        task = deployed_k8s.run(_jaime_unit(), "show-status")
        assert task.success

    def test_reads_watched_app_status_from_controller(self, deployed_k8s):
        """Statuses come from the controller API, not from any relation."""
        task = deployed_k8s.run(_jaime_unit(), "show-status")
        assert task.success
        # Either a unit of the watched app is reported, or nothing has been
        # observed yet; a controller auth failure would have blocked instead.
        observed = task.results.get("unit", "") or task.results.get("result", "")
        assert WATCHED_APP in observed or "no status observed" in observed


class TestRejectedControllerCredentials:
    """Bad Juju credentials must block, not fail silently."""

    def test_wrong_password_blocks_the_unit(self, deployed_k8s):
        deployed_k8s.config(K8S_APP_NAME, {"juju-api-password": "definitely-wrong"})
        deployed_k8s.wait(
            lambda s: jubilant.all_blocked(s, K8S_APP_NAME),
            timeout=10 * 60,
        )

    def test_restoring_password_recovers(self, deployed_k8s, observer_credentials):
        _, password = observer_credentials
        deployed_k8s.config(K8S_APP_NAME, {"juju-api-password": password})
        deployed_k8s.wait(
            lambda s: jubilant.all_active(s, K8S_APP_NAME),
            timeout=10 * 60,
        )

    def test_unknown_user_blocks_the_unit(self, deployed_k8s, observer_credentials):
        user, password = observer_credentials
        deployed_k8s.config(K8S_APP_NAME, {"juju-api-user": "no-such-user"})
        deployed_k8s.wait(
            lambda s: jubilant.all_blocked(s, K8S_APP_NAME),
            timeout=10 * 60,
        )
        deployed_k8s.config(K8S_APP_NAME, {"juju-api-user": user})
        deployed_k8s.wait(
            lambda s: jubilant.all_active(s, K8S_APP_NAME),
            timeout=10 * 60,
        )


class TestMissingRbac:
    """Without the RoleBinding the prerequisite check (TASKS 4.2) must block.

    Previously the charm degraded silently to empty log/event sections; the
    explicit RBAC preflight replaced that with a blocked status naming the
    missing prerequisite. Applying the RoleBinding must lift the unit back to
    active.
    """

    def test_missing_rbac_blocks_the_unit(self, deployed_k8s):
        _apply_rbac(deployed_k8s, remove=True)
        deployed_k8s.wait(
            lambda s: jubilant.all_blocked(s, K8S_APP_NAME),
            timeout=10 * 60,
        )
        message = deployed_k8s.status().get_units(K8S_APP_NAME)[
            _jaime_unit()
        ].workload_status.message
        assert "RBAC" in message

    def test_restoring_rbac_recovers(self, deployed_k8s):
        _apply_rbac(deployed_k8s)
        deployed_k8s.wait(
            lambda s: jubilant.all_active(s, K8S_APP_NAME),
            timeout=10 * 60,
        )


class TestNoProviderFallback:
    def test_act_mode_is_blocked_as_not_implemented(self, deployed_k8s):
        deployed_k8s.config(K8S_APP_NAME, {"mode": "act"})
        deployed_k8s.wait(lambda s: jubilant.all_blocked(s, K8S_APP_NAME))

        status = deployed_k8s.status()
        unit_info = status.get_units(K8S_APP_NAME)[_jaime_unit()]
        assert "not yet implemented" in unit_info.workload_status.message

        deployed_k8s.config(K8S_APP_NAME, {"mode": "observe"})
        deployed_k8s.wait(lambda s: jubilant.all_active(s, K8S_APP_NAME))

    def test_observe_mode_active_without_provider(self, deployed_k8s):
        deployed_k8s.config(K8S_APP_NAME, {"mode": "observe", "provider": "none"})
        deployed_k8s.wait(lambda s: jubilant.all_active(s, K8S_APP_NAME))
