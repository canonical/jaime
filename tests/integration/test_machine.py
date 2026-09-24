"""Integration tests for the Jaime machine subordinate charm.

Covers TASKS.md 5.2: deploy, inject a fault, and assert the
incident -> report -> suggestion chain, plus the non-AI fallback path.

Requires a bootstrapped machine controller (LXD).
"""

import json

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
def deployed(juju, machine_charm, ai_provider, ai_token):
    """Deploy the principal and relate Jaime to it.

    Provider config is applied BEFORE the relation is created. The diagnostics
    plan is generated once on principal-relation-joined and config-changed does
    not regenerate it, so relating first would produce an empty plan.
    """
    juju.deploy(PRINCIPAL_APP, channel=PRINCIPAL_CHANNEL, base=PRINCIPAL_BASE)

    config = {
        "mode": "suggest" if ai_token else "observe",
        "provider": ai_provider if ai_token else "none",
        "failure-timeout-minutes": FAILURE_TIMEOUT_MINUTES,
        "cooldown-minutes": 1,
        "watch-statuses": "error,blocked",
    }
    if ai_token:
        config["api-token"] = ai_token

    juju.deploy(machine_charm, JAIME_APP, config=config)

    # Principal must settle before relating, otherwise the first status Jaime
    # observes is transient install churn rather than a real fault.
    juju.wait(lambda s: jubilant.all_active(s, PRINCIPAL_APP))

    juju.integrate(JAIME_APP, PRINCIPAL_APP)
    juju.wait(lambda s: jubilant.all_active(s, PRINCIPAL_APP, JAIME_APP))
    return juju


class TestDeployment:
    def test_jaime_is_active_when_principal_healthy(self, deployed):
        status = deployed.status()
        jaime = status.apps[JAIME_APP]
        assert jaime.app_status.current == "active"

    def test_jaime_is_subordinate_to_principal(self, deployed):
        """The subordinate must be co-located, not on its own machine."""
        status = deployed.status()
        assert status.apps[JAIME_APP].subordinate_to == [PRINCIPAL_APP]

    def test_show_status_action_reports_no_incident(self, deployed):
        task = deployed.run(jaime_unit(deployed), "show-status")
        assert task.success

    def test_diagnostics_plan_generated_on_relation(self, deployed, ai_token):
        """The plan is AI-generated on relation-joined, even in observe mode."""
        if not ai_token:
            pytest.skip("no AI token configured; diagnostics plan is not generated")
        unit = jaime_unit(deployed)
        result = deployed.ssh(unit, "cat /var/lib/jaime/diagnostics.json")
        plan = json.loads(result)
        assert "monitoring_plan" in plan


class TestIncidentLifecycle:
    """Drive the principal into a watched status and assert the chain."""

    def test_fault_opens_incident_and_writes_report(self, deployed):
        unit = jaime_unit(deployed)

        # Inject a fault by driving the principal into a watched status.
        set_principal_status(deployed, "blocked", "integration test fault")
        deployed.wait(
            lambda s: principal_status(s) == "blocked",
            timeout=5 * 60,
        )

        # Wait past failure-timeout-minutes for the incident to open, then
        # for Jaime to report it. update-status drives the loop, so this is
        # bounded by the model's update-status-hook-interval, which conftest
        # shortens to 60s.
        deployed.wait(
            lambda s: "incident open" in jaime_message(s, unit),
            timeout=15 * 60,
        )

        assert show_status(deployed, unit, PRINCIPAL_UNIT)["incident-id"]

    def test_report_file_exists_and_references_incident(self, deployed):
        unit = jaime_unit(deployed)
        incident_id = show_status(deployed, unit, PRINCIPAL_UNIT)["incident-id"]

        listing = deployed.ssh(unit, "ls /var/log/jaime/reports/")
        assert incident_id in listing

        report = deployed.ssh(unit, f"cat /var/log/jaime/reports/{incident_id}.md")
        assert incident_id in report
        # Raw collected evidence must always be retained, AI or not.
        assert "Recent unit logs" in report

    def test_audit_log_records_incident_start(self, deployed):
        unit = jaime_unit(deployed)
        raw = deployed.ssh(unit, "cat /var/log/jaime/events.jsonl")
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        kinds = {e.get("event") for e in events}
        assert "incident-start" in kinds
        assert "report-generated" in kinds

    def test_audit_log_never_contains_the_api_token(self, deployed, ai_token):
        """Tokens must never reach the audit log or reports."""
        if not ai_token:
            pytest.skip("no AI token configured")
        unit = jaime_unit(deployed)
        raw = deployed.ssh(unit, "cat /var/log/jaime/events.jsonl")
        assert ai_token not in raw

        reports = deployed.ssh(unit, "cat /var/log/jaime/reports/*.md")
        assert ai_token not in reports

    def test_get_suggestion_returns_advice(self, deployed, ai_token):
        if not ai_token:
            pytest.skip("no AI token configured; suggest mode unavailable")
        unit = jaime_unit(deployed)
        task = deployed.run(unit, "get-suggestion", wait=300)
        assert task.success
        assert task.results.get("description")
        assert task.results.get("incident-id")

    def test_get_suggestion_is_cached_on_repeat(self, deployed, ai_token):
        """A second call with identical context must not re-invoke the provider."""
        if not ai_token:
            pytest.skip("no AI token configured; suggest mode unavailable")
        unit = jaime_unit(deployed)
        task = deployed.run(unit, "get-suggestion", wait=300)
        assert task.success
        assert task.results.get("cached") == "true"

    def test_recovery_closes_incident(self, deployed):
        unit = jaime_unit(deployed)
        set_principal_status(deployed, "active", "recovered")
        # Jaime holds ActiveStatus while an incident is open, so waiting for
        # all_active would be satisfied instantly and assert against a stale
        # message. Wait for the incident to actually close, which Jaime does on
        # its next update-status after seeing the principal leave a watched
        # status.
        deployed.wait(
            lambda s: (
                principal_status(s) == "active"
                and "incident open" not in jaime_message(s, unit)
            ),
            timeout=15 * 60,
        )
        task = deployed.run(unit, "show-status")
        assert task.success
        assert "incident open" not in jaime_message(deployed.status(), unit)


class TestNoProviderFallback:
    """The charm must produce a useful report with no AI provider configured."""

    def test_observe_mode_active_without_provider(self, deployed):
        deployed.config(JAIME_APP, {"mode": "observe", "provider": "none"})
        deployed.wait(lambda s: jubilant.all_active(s, JAIME_APP))

    def test_act_mode_is_blocked_as_not_implemented(self, deployed):
        deployed.config(JAIME_APP, {"mode": "act"})
        deployed.wait(lambda s: jubilant.all_blocked(s, JAIME_APP))

        status = deployed.status()
        unit = jaime_unit(deployed)
        assert "not yet implemented" in jaime_message(status, unit)

        # Restore so later tests are unaffected.
        deployed.config(JAIME_APP, {"mode": "observe"})
        deployed.wait(lambda s: jubilant.all_active(s, JAIME_APP))

    def test_invalid_mode_blocks(self, deployed):
        deployed.config(JAIME_APP, {"mode": "nonsense"})
        deployed.wait(lambda s: jubilant.all_blocked(s, JAIME_APP))
        deployed.config(JAIME_APP, {"mode": "observe"})
        deployed.wait(lambda s: jubilant.all_active(s, JAIME_APP))
