"""Shared fixtures for Jaime integration tests.

These tests drive a real Juju controller. They are excluded from the default
unit sweep (root ``pyproject.toml`` sets ``testpaths = ["tests/unit"]``) and
must be invoked explicitly:

    make integration          # both substrates
    make integration-machine  # machine subordinate only
    make integration-k8s      # Kubernetes standalone only

Each substrate needs an appropriate controller to be bootstrapped and
selected: a machine cloud (LXD) for the machine charm, a Kubernetes cloud
(MicroK8s) for the k8s charm.
"""

import json
import os
import pathlib

import pytest

# jubilant is declared in tests/integration/requirements.txt and is not
# installed for the unit suites. Degrade gracefully rather than breaking
# collection, so that pointing pytest at tests/ instead of tests/unit/
# skips this directory instead of failing the whole run.
try:
    import jubilant
except ImportError:  # pragma: no cover
    jubilant = None
    collect_ignore_glob = ["test_*.py"]

DIST_DIR = pathlib.Path(__file__).resolve().parents[2] / "dist"

MACHINE_CHARM_GLOB = "jaime_*.charm"
K8S_CHARM_GLOB = "jaime-k8s_*.charm"

# The k8s RoleBinding in charms/k8s/jaime-k8s-rbac.yaml is bound to the
# jaime-k8s ServiceAccount, which Juju names after the application. Deploying
# under any other name yields empty log and event sections.
K8S_APP_NAME = "jaime-k8s"

# Kept short so incidents open within the lifetime of a test rather than the
# five-minute production default.
FAILURE_TIMEOUT_MINUTES = 1

JAIME_APP = "jaime"

# The Jaime machine charm is built for ubuntu@24.04 only, and Juju refuses to
# relate a subordinate to a principal whose base it does not support:
#
#   ERROR cannot add relation "jaime:principal <app>:juju-info": subordinate
#   must support principal application's base
#
# So the principal must also be on 24.04. That rules out mysql 8.0/stable and
# postgresql 14/stable, which are both jammy; postgresql 16/stable is noble.
#
# any-charm is used rather than a real workload because its status can be
# driven deterministically via set_principal_status, so the tests do not
# depend on a particular charm's internal service names or on which status
# that charm happens to report when it degrades. Covering a realistic
# principal is tracked separately in TASKS.md 5.2.
PRINCIPAL_APP = "any-charm"
PRINCIPAL_CHANNEL = "latest/beta"
PRINCIPAL_BASE = "ubuntu@24.04"
PRINCIPAL_UNIT = f"{PRINCIPAL_APP}/0"


def jaime_unit(juju) -> str:
    """Return the name of the subordinate Jaime unit.

    Subordinate units are not listed under ``status.apps[app].units``, which
    is empty for a subordinate. Juju nests them under the principal unit's
    ``subordinates`` map, and ``Status.get_units`` resolves that via the app's
    ``subordinate_to`` list.
    """
    units = juju.status().get_units(JAIME_APP)
    assert units, "Jaime subordinate has no units"
    return next(iter(units))


def jaime_message(status, unit: str) -> str:
    """Workload status message of the Jaime subordinate unit, or ""."""
    unit_info = status.get_units(JAIME_APP).get(unit)
    return unit_info.workload_status.message if unit_info else ""


def principal_status(status) -> str:
    """Current workload status of the principal unit, or ""."""
    unit_info = status.get_units(PRINCIPAL_APP).get(PRINCIPAL_UNIT)
    return unit_info.workload_status.current if unit_info else ""


def set_principal_status(juju, status: str, message: str) -> None:
    """Drive the principal's workload status via the status-set hook tool.

    Uses `juju exec` rather than breaking a real workload, so status changes
    are immediate and deterministic. Each call also bumps Juju's `since`
    field, which is exactly what the flapping tests need to exercise.
    """
    juju.exec("status-set", status, message, unit=PRINCIPAL_UNIT)


def show_status_records(juju, jaime_app_unit: str) -> list[dict]:
    """Run show-status on the Jaime unit and return the parsed per-unit records.

    The action returns a JSON array under ``result`` because Juju action
    results are a flat map of scalars and a charm may monitor several units.
    """
    task = juju.run(jaime_app_unit, "show-status")
    assert task.success, "show-status action failed"
    return json.loads(task.results["result"])


def show_status(juju, jaime_app_unit: str, unit: str) -> dict:
    """Return the show-status record for one monitored unit ({} if absent)."""
    for record in show_status_records(juju, jaime_app_unit):
        if record.get("unit") == unit:
            return record
    return {}


def _charm_path(glob: str) -> pathlib.Path:
    """Locate a packed charm, failing with a build hint if it is missing."""
    matches = sorted(DIST_DIR.glob(glob))
    if not matches:
        pytest.fail(
            f"No charm matching {glob!r} in {DIST_DIR}. Run `make pack-all` first."
        )
    return matches[-1]


def pytest_addoption(parser):
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="Do not tear down the temporary Juju model, for post-mortem debugging.",
    )


@pytest.fixture(scope="module")
def keep_models(request) -> bool:
    return bool(request.config.getoption("--keep-models"))


@pytest.fixture(scope="module")
def juju(keep_models):
    """A temporary Juju model, torn down at the end of the module.

    Jaime's incident lifecycle is driven by ``update-status``. Juju's default
    hook interval is 5 minutes, which would make every incident assertion wait
    minutes and sit close to the test timeouts. Shortening it to 60s keeps the
    suite both faster and more reliable; it does not change the behaviour
    under test, only how often the loop is ticked.
    """
    with jubilant.temp_model(
        keep=keep_models,
        config={"update-status-hook-interval": "60s"},
    ) as juju:
        juju.wait_timeout = 10 * 60
        yield juju
        if keep_models:
            print(f"\nKeeping model {juju.model!r} for inspection.")


@pytest.fixture(scope="module")
def machine_charm() -> pathlib.Path:
    return _charm_path(MACHINE_CHARM_GLOB)


@pytest.fixture(scope="module")
def k8s_charm() -> pathlib.Path:
    return _charm_path(K8S_CHARM_GLOB)


@pytest.fixture(scope="session")
def ai_token() -> str:
    """AI provider token, or empty when unset.

    Tests that need a real provider skip when this is empty, so the suite
    stays runnable offline. The token is never written to a report or log.
    """
    return os.environ.get("JAIME_TEST_API_TOKEN", "")


@pytest.fixture(scope="session")
def ai_provider() -> str:
    return os.environ.get("JAIME_TEST_PROVIDER", "gemini")
