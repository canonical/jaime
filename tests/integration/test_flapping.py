"""Integration test for the flapping-workload case.

Covers TASKS.md 5.2. Jaime anchors the incident timer on when it first
observed a unit go unhealthy (``unhealthy_since``), not on Juju's ``since``
field. Juju bumps ``since`` every time a charm re-sets its status, including
re-setting the same status with a new message, so a workload stuck in a retry
loop would otherwise reset the timer forever and never reach
``failure-timeout-minutes``.

The unit suite guards this logic directly; this test proves it survives a
real Juju status stream.

Requires a bootstrapped machine controller (LXD).
"""

import jubilant
import pytest

from .conftest import (
    FAILURE_TIMEOUT_MINUTES,
    JAIME_APP,
    PRINCIPAL_APP,
    PRINCIPAL_BASE,
    PRINCIPAL_CHANNEL,
    PRINCIPAL_UNIT,
    jaime_message,
    jaime_unit,
    principal_status,
    set_principal_status,
    show_status,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def flapping_model(juju, machine_charm):
    juju.deploy(PRINCIPAL_APP, channel=PRINCIPAL_CHANNEL, base=PRINCIPAL_BASE)
    juju.deploy(
        machine_charm,
        JAIME_APP,
        config={
            "mode": "observe",
            "provider": "none",
            "failure-timeout-minutes": FAILURE_TIMEOUT_MINUTES,
            "cooldown-minutes": 1,
            # maintenance is watched alongside blocked so the flapping test can
            # move between two watched statuses using only values status-set
            # accepts. `error` is not settable; it is a Juju agent state.
            "watch-statuses": "blocked,maintenance",
        },
    )
    juju.wait(lambda s: jubilant.all_active(s, PRINCIPAL_APP))
    juju.integrate(JAIME_APP, PRINCIPAL_APP)
    juju.wait(lambda s: jubilant.all_active(s, PRINCIPAL_APP, JAIME_APP))
    return juju


class TestFlappingWorkload:
    def test_repeated_status_resets_do_not_defer_incident(self, flapping_model):
        """Re-setting blocked with a new message must not restart the timer.

        Each re-set bumps Juju's `since`. If the timer were anchored there,
        the incident would never open. Anchored on `unhealthy_since`, it does.
        """
        juju = flapping_model
        unit = jaime_unit(juju)

        # Flap the status repeatedly across a span longer than the timeout.
        for i in range(6):
            set_principal_status(juju, "blocked", f"retry attempt {i}")
            juju.wait(
                lambda s: principal_status(s) == "blocked",
                timeout=120,
            )

        # Despite `since` having been bumped on every iteration, the incident
        # must still open once failure-timeout-minutes has elapsed since the
        # FIRST unhealthy observation.
        juju.wait(
            lambda s: "incident open" in jaime_message(s, unit),
            timeout=15 * 60,
        )

        assert show_status(juju, unit, PRINCIPAL_UNIT)["incident-id"]

    def test_first_seen_precedes_status_since(self, flapping_model):
        """show-status must expose both anchors, with first-seen the older."""
        juju = flapping_model
        record = show_status(juju, jaime_unit(juju), PRINCIPAL_UNIT)

        first_seen = record.get("first-seen", "")
        status_since = record.get("status-since", "")
        assert first_seen and status_since
        # first-seen is when Jaime first saw the unit unhealthy; status-since
        # is Juju's last status bump, which the flapping above kept refreshing.
        assert first_seen <= status_since

    def test_flapping_between_watched_statuses_keeps_one_incident(self, flapping_model):
        """Flapping between two watched statuses must not open a second incident.

        Uses blocked <-> maintenance rather than blocked <-> error: `error` is
        a Juju agent state produced by a failed hook, not a workload status,
        so status-set cannot produce it. Both statuses here are in the
        fixture's watch-statuses, which is what the assertion depends on.
        """
        juju = flapping_model
        unit = jaime_unit(juju)

        before = show_status(juju, unit, PRINCIPAL_UNIT).get("incident-id")
        assert before, "expected an incident to already be open"

        set_principal_status(juju, "maintenance", "restarting")
        juju.wait(lambda s: principal_status(s) == "maintenance", timeout=120)

        set_principal_status(juju, "blocked", "still broken")
        juju.wait(lambda s: principal_status(s) == "blocked", timeout=120)

        juju.wait(
            lambda s: "incident open" in jaime_message(s, unit),
            timeout=10 * 60,
        )

        after = show_status(juju, unit, PRINCIPAL_UNIT).get("incident-id")
        assert after == before, "flapping between watched statuses opened a new incident"
