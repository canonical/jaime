#!/usr/bin/env python3
"""Jaime K8s charm — monitors Kubernetes application units in the same model.

Unlike the machine subordinate, this charm:
- runs as its own pod (not co-located with any principal)
- reads unit workload statuses from the Juju controller API using a
  dedicated Juju user account
- collects diagnostics from the Kubernetes API using the pod's in-cluster
  service account
"""

import datetime
import json
import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from jaime.collector import collect_context
from jaime.controller import (
    ControllerAuthError,
    ControllerError,
    extract_app_metadata,
    extract_unit_statuses,
)
from jaime.core import CREDENTIALS_REJECTED, CREDENTIALS_REQUIRED, CoreMixin
from jaime.diagnostics import validate_k8s_diagnostics
from jaime.k8s_api import K8sApiClient
from jaime.names import app_of
from jaime.principal import StatusTracker
from jaime.timeutil import utc_now_iso

logger = logging.getLogger(__name__)


class JaimeK8sCharm(CoreMixin, CharmBase):
    """Jaime K8s charm — standalone pod monitoring other applications."""

    def __init__(self, *args):
        super().__init__(*args)
        self._status_tracker = StatusTracker()

        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)

        self.framework.observe(self.on.show_status_action, self._on_action_show_status)
        self.framework.observe(self.on.show_usage_action, self._on_action_show_usage)
        self.framework.observe(self.on.show_setup_steps_action, self._on_action_show_setup_steps)
        self.framework.observe(self.on.get_suggestion_action, self._on_action_get_suggestion)
        self.framework.observe(self.on.ask_action, self._on_action_ask)
        self.framework.observe(self.on.list_suggestions_action, self._on_action_list_suggestions)
        self.framework.observe(self.on.generate_report_action, self._on_action_generate_report)
        self.framework.observe(self.on.list_incidents_action, self._on_action_list_incidents)
        self.framework.observe(self.on.reset_action, self._on_action_reset)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _on_update_status(self, event):
        with self._hook_cache():
            self._monitor()

    def _substrate_config_error(self) -> str | None:
        """Block on an invalid diagnostics plan, auditing the rejection."""
        plan, diag_error = self._parse_diagnostics_config()
        if diag_error:
            self._audit("diagnostics-config-invalid", unit=self.unit.name, error=diag_error)
            return f"invalid diagnostics config: {diag_error}"
        if plan:
            unknown = sorted(set(plan) - set(self._watch_applications()))
            if unknown:
                logger.debug(
                    "diagnostics plan for unmonitored applications ignored: %s",
                    ", ".join(unknown),
                )
        return None

    def _parse_diagnostics_config(self) -> tuple[dict | None, str | None]:
        """Return (plan, error) for the keyed k8s diagnostics config.

        ``plan`` is the parsed object keyed by application name, or ``None``
        when the option is empty or invalid. ``error`` is the first validation
        error, or ``None``. Kept separate from collection so an invalid plan
        fails safe instead of reaching the collector.
        """
        raw = self.model.config.get("diagnostics", "")
        if not raw:
            return None, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"not valid JSON: {e}"
        errors = validate_k8s_diagnostics(parsed)
        if errors:
            return None, errors[0]
        return parsed, None

    def _app_diagnostics_plan(self, app_name: str) -> dict | None:
        """Return the plan for one application, or None when it has none."""
        plan, _ = self._parse_diagnostics_config()
        if not plan:
            return None
        return plan.get(app_name)

    def _monitored_applications(self) -> list[str]:
        """Applications resolved as monitored, derived from persisted state.

        Only applications that have an observed unit and are still listed in
        watch-applications count; there is no wildcard on this substrate.
        Deriving from the tracker, rather than an in-memory per-hook cache,
        keeps the answer correct outside a monitoring cycle.
        """
        watch = set(self._watch_applications())
        tracked = {app_of(name) for name in self._status_tracker.units()}
        return sorted(app for app in tracked if app in watch)

    def _prerequisite_error(self) -> str | None:
        """Return a blocked-status message while prerequisites are unmet.

        Checks in order: (1) observer credentials are configured, (2) they
        authenticate against the controller, (3) the in-cluster service
        account can read the Kubernetes API, and (4) every
        ``watch-applications`` name exists on the model. Absent names are
        blocked as an explicit preflight instead of the old silent
        "no units matched" status.

        The connectivity checks (credentials, controller auth, Kubernetes API)
        run whether or not ``watch-applications`` is set: the charm must not
        report ready merely because nothing is monitored. Returns None when
        everything is verified, or when the state is only transiently
        unverifiable (controller/API unreachable, agent.conf not yet present)
        so bootstrap never blocks on a hiccup; the monitoring status path
        reports those.
        """

        # 1. Observer credentials must be configured.
        if not self._has_controller_credentials():
            return CREDENTIALS_REQUIRED

        # 2. The controller must be reachable and the credentials accepted.
        #    The FullStatus fetched here is reused by the rest of the hook.
        try:
            full = self._full_status()
        except ControllerAuthError:
            return CREDENTIALS_REJECTED
        except Exception as e:
            logger.debug("controller prerequisite check failed: %s", e)
            return None

        # 3. The in-cluster service account must be able to read the
        #    Kubernetes API (RoleBinding applied).
        try:
            k8s = K8sApiClient()
        except Exception as e:
            logger.debug("k8s service account not available: %s", e)
            return None
        kind, detail = k8s.check_access()
        if kind == "forbidden":
            return f"Kubernetes RBAC missing: {detail}"
        if kind == "unreachable":
            logger.debug("k8s API not reachable yet: %s", detail)
            return None

        # 4. Last: every watch-applications name must exist on the model.
        present = set(full.get("applications") or {})
        missing = [a for a in self._watch_applications() if a not in present]
        if missing:
            return "watch-applications not found on model: " + ", ".join(missing)
        return None

    def _monitor(self):
        """Fetch statuses and drive the incident lifecycle for each unit."""
        # An invalid diagnostics plan blocks before anything else, so
        # update-status cannot overwrite the blocked state with "Ready".
        _, diag_error = self._parse_diagnostics_config()
        if diag_error:
            self.unit.status = BlockedStatus(f"invalid diagnostics config: {diag_error}")
            return

        # Prerequisites first, whether or not anything is monitored: the charm
        # must not report ready while the controller, RBAC, or Kubernetes API
        # is in a bad state. Only after that does opt-out apply.
        prereq = self._prerequisite_error()
        if prereq:
            self.unit.status = BlockedStatus(prereq)
            return

        # Monitoring is opt-in: an empty watch-applications list means nothing
        # is monitored, never "all applications in the model".
        if not self._watch_applications():
            self.unit.status = ActiveStatus("Ready: no apps in watch-applications")
            return

        try:
            statuses = self._fetch_unit_statuses()
        except ControllerError as e:
            self.unit.status = self._controller_error_status(e)
            return

        if not statuses:
            self.unit.status = MaintenanceStatus("no units matched watch-applications")
            return

        now_iso = utc_now_iso()
        self._process_units(
            [
                (unit_name, info["status"], info["since"] or now_iso, info.get("message", ""))
                for unit_name, info in statuses.items()
            ]
        )

    # ------------------------------------------------------------------
    # Juju controller access
    # ------------------------------------------------------------------

    def _fetch_unit_statuses(self) -> dict[str, dict]:
        """Fetch per-unit workload statuses from the Juju controller API."""
        return extract_unit_statuses(
            self._full_status(),
            watch_applications=self._watch_applications(),
        )

    def _fetch_app_config(self, app_name: str) -> dict:
        """Fetch an application's current config from the controller API."""
        return self._controller_query(
            lambda client: client.application_get(app_name).get("config", {}),
            {},
            f"config for {app_name}",
        )

    def _fetch_app_metadata(self, app_name: str) -> dict:
        """Fetch an application's charm metadata for the report header."""
        return self._full_status_query(
            lambda full_status: extract_app_metadata(full_status, app_name),
            {},
            f"app metadata for {app_name}",
        )

    # ------------------------------------------------------------------
    # Setup guidance
    # ------------------------------------------------------------------

    def _setup_guide(self) -> str:
        """Return a copy-paste shell guide for configuring this charm.

        Covers the four hard areas: Kubernetes RBAC, the read-only Juju user
        for the controller API, the observer password and AI token as Juju
        secrets granted to the application, and the charm config that drives
        monitoring. All commands run on an operator machine with Juju and
        kubectl access; the charm never executes them.

        The model name is read at runtime (``JUJU_MODEL_NAME``), so the guide
        arrives pre-filled for the model the charm is deployed in.
        """
        app = self.app.name
        model = self.model.name or "<your-model-name>"
        rbac_url = "https://raw.githubusercontent.com/canonical/jaime/main/charms/k8s/jaime-k8s-rbac.yaml"
        return "\n".join(
            (
                "# Jaime k8s setup guide: run on a machine with Juju and kubectl access.",
                "# Replace the <...> placeholders first.",
                "",
                f"MODEL_NAME={model}",
                "",
                "# 1. Kubernetes RBAC: pod logs/events/metrics read access. The required Role/RoleBinding live",
                "#    in the repository and are applied straight from there.",
                f"kubectl apply -f {rbac_url} -n ${{MODEL_NAME}}",
                "",
                "# 2. Read-only Juju user for the controller API.",
                "juju add-user jaime-observer",
                "juju grant jaime-observer read ${MODEL_NAME}",
                "# Generate a password on the spot, or set your own here.",
                "NEW_PASS=$(openssl rand -hex 16)",
                'echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt',
                "",
                "# 3. Pass the observer credentials to the charm as a Juju secret.",
                'SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")',
                f"juju grant-secret jaime-juju-api {app}",
                f'juju config {app} juju-api-user=jaime-observer juju-api-password="${{SECRET_URI}}"',
                "",
                "# 4. Choose which applications to monitor. Monitoring is opt-in:",
                "#    empty watches nothing, never everything.",
                f"juju config {app} watch-applications=postgresql-k8s,mysql-k8s",
                "",
                "# 5. Optional: enable AI suggestions. Grant the token secret to the application",
                "AI_SECRET=$(juju add-secret jaime-token token=<your-api-token>)",
                f"juju grant-secret jaime-token {app}",
                f'juju config {app} mode=suggest provider=openrouter api-token="${{AI_SECRET}}"',
            )
        )

    def _on_action_show_setup_steps(self, event):
        """Emit the exact setup steps for this charm. Read-only."""
        event.set_results({"result": self._setup_guide()})

    # ------------------------------------------------------------------
    # Substrate hooks used by CoreMixin
    # ------------------------------------------------------------------

    def _collect_context(self, unit_name: str, from_time: datetime.datetime | None) -> dict:
        """Collect Kubernetes diagnostic context (pod logs, spec, events)."""
        app_name = app_of(unit_name)
        context = collect_context(
            unit_name,
            self.model.config.get("log-window-minutes", 30),
            self.model.config.get("max-context-lines", 500),
            from_time=from_time,
            diagnostics_plan=self._app_diagnostics_plan(app_name),
        )
        context["juju_config"] = self._fetch_app_config(app_name)
        context["app_metadata"] = self._fetch_app_metadata(app_name)
        return context


if __name__ == "__main__":
    main(JaimeK8sCharm)
