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
import logging

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from jaime.collector import collect_context
from jaime.controller import (
    ControllerAuthError,
    ControllerError,
    JujuControllerClient,
    agent_conf_path,
    extract_unit_statuses,
    parse_agent_conf,
)
from jaime.core import CoreMixin
from jaime.incident import Incident
from jaime.k8s_api import K8sApiClient
from jaime.principal import StatusTracker

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
        self.framework.observe(self.on.generate_report_action, self._on_action_generate_report)
        self.framework.observe(self.on.reset_action, self._on_action_reset)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _on_update_status(self, event):
        self._monitor()

    def _on_config_changed(self, event):
        super()._on_config_changed(event)
        if isinstance(self.unit.status, BlockedStatus):
            return
        prereq = self._prerequisite_error()
        if prereq:
            self.unit.status = BlockedStatus(prereq)

    def _watch_applications(self) -> list[str]:
        raw = self.model.config.get("watch-applications", "")
        return [a.strip() for a in raw.split(",") if a.strip()]

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
        username = self.model.config.get("juju-api-user", "")
        password = self._resolve_juju_password()
        if not username or not password:
            return "juju-api-user and juju-api-password must be configured"

        # 2. The controller must be reachable and the credentials accepted.
        conf_path = agent_conf_path(unit_name=self.unit.name)
        if conf_path is None:
            return None
        try:
            conf = parse_agent_conf(conf_path)
            with JujuControllerClient(
                conf["api_address"], conf["ca_cert"], conf["model_uuid"]
            ) as client:
                client.login(username, password)
                full = client.full_status()
        except ControllerAuthError:
            return "juju-api credentials rejected by controller"
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
            return (
                "watch-applications not found on model: "
                + ", ".join(missing)
            )
        return None

    def _resolve_juju_password(self) -> str:
        """Resolve the Juju API password from config (plain or secret URI)."""
        return self._resolve_secret(
            self.model.config.get("juju-api-password", ""), "password"
        )

    def _monitor(self):
        """Fetch statuses and drive the incident lifecycle for each unit."""
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
            self.unit.status = ActiveStatus(
                "Ready: no apps in watch-applications"
            )
            return

        try:
            statuses = self._fetch_unit_statuses()
        except ControllerAuthError as e:
            logger.error("Juju controller authentication failed: %s", e)
            self.unit.status = BlockedStatus(
                "juju-api credentials rejected by controller"
            )
            return
        except ControllerError as e:
            logger.warning("could not fetch unit statuses: %s", e)
            self.unit.status = MaintenanceStatus(str(e)[:100])
            return

        if not statuses:
            self.unit.status = MaintenanceStatus("no units matched watch-applications")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        for unit_name, info in statuses.items():
            status = info["status"]
            since_iso = info["since"] or now.isoformat()
            self._process_unit(unit_name, status, since_iso)

    # ------------------------------------------------------------------
    # Juju controller access
    # ------------------------------------------------------------------

    def _fetch_unit_statuses(self) -> dict[str, dict]:
        """Fetch per-unit workload statuses from the Juju controller API."""
        username = self.model.config.get("juju-api-user", "")
        password = self._resolve_juju_password()
        if not username or not password:
            raise ControllerError(
                "juju-api-user and juju-api-password must be configured"
            )

        conf_path = agent_conf_path(unit_name=self.unit.name)
        if conf_path is None:
            raise ControllerError("agent.conf not found: cannot locate controller")

        conf = parse_agent_conf(conf_path)
        with JujuControllerClient(
            conf["api_address"], conf["ca_cert"], conf["model_uuid"]
        ) as client:
            client.login(username, password)
            full = client.full_status()
        return extract_unit_statuses(
            full,
            watch_applications=self._watch_applications(),
        )

    def _fetch_app_config(self, app_name: str) -> dict:
        """Fetch an application's current config from the controller API."""
        username = self.model.config.get("juju-api-user", "")
        password = self._resolve_juju_password()
        if not username or not password:
            return {}
        conf_path = agent_conf_path(unit_name=self.unit.name)
        if conf_path is None:
            return {}
        try:
            conf = parse_agent_conf(conf_path)
            with JujuControllerClient(
                conf["api_address"], conf["ca_cert"], conf["model_uuid"]
            ) as client:
                client.login(username, password)
                info = client.application_get(app_name)
            return info.get("config", {})
        except Exception as e:
            logger.debug("could not fetch config for %s: %s", app_name, e)
            return {}

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
        rbac_url = (
            "https://raw.githubusercontent.com/canonical/jaime/main/"
            "charms/k8s/jaime-k8s-rbac.yaml"
        )
        return "\n".join((
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
        ))

    def _on_action_show_setup_steps(self, event):
        """Emit the exact setup steps for this charm. Read-only."""
        event.set_results({"result": self._setup_guide()})

    # ------------------------------------------------------------------
    # Substrate hooks used by CoreMixin
    # ------------------------------------------------------------------

    def _collect_incident_context(self, unit_name: str, since_iso: str,
                                  incident: Incident) -> dict:
        """Collect Kubernetes diagnostic context (pod logs, spec, events)."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        try:
            since_dt = datetime.datetime.fromisoformat(since_iso)
        except ValueError:
            since_dt = None

        context = collect_context(
            unit_name, log_window, max_lines, from_time=since_dt,
        )
        app_name = unit_name.split("/")[0]
        context["juju_config"] = self._fetch_app_config(app_name)
        return context

    def _collect_report_context(self, unit_name: str, since_iso: str) -> dict:
        """Collect context for a manually regenerated report."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        try:
            since_dt = datetime.datetime.fromisoformat(since_iso)
        except ValueError:
            since_dt = None
        context = collect_context(
            unit_name, log_window, max_lines, from_time=since_dt,
        )
        app_name = unit_name.split("/")[0]
        context["juju_config"] = self._fetch_app_config(app_name)
        return context


if __name__ == "__main__":
    main(JaimeK8sCharm)
