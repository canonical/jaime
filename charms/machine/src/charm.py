#!/usr/bin/env python3
"""Jaime charm — diagnostics plan generation on relation-joined."""

import datetime
import json
import logging

from ops import JujuContext
from ops.charm import CharmBase
from ops.hookcmds import goal_state
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
from jaime.diagnostics import (
    build_prompt,
    make_empty_plan,
    read_diagnostics_file,
    validate_diagnostics,
    write_diagnostics_file,
)
from jaime.incident import Incident
from jaime.logging import write_event
from jaime.principal import StatusTracker

logger = logging.getLogger(__name__)


class JaimeCharm(CoreMixin, CharmBase):
    _diagnostics_dir = "/var/lib/jaime"
    _diagnostics_path = f"{_diagnostics_dir}/diagnostics.json"

    def __init__(self, *args):
        super().__init__(*args)
        self._status_tracker = StatusTracker()

        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.principal_relation_changed, self._on_principal_changed)
        self.framework.observe(self.on.principal_relation_joined, self._on_principal_joined)
        self.framework.observe(self.on.principal_relation_broken, self._on_principal_broken)

        self.framework.observe(self.on.diagnose_action, self._on_action_diagnose)
        self.framework.observe(self.on.collect_context_action, self._on_action_collect_context)
        self.framework.observe(self.on.generate_report_action, self._on_action_generate_report)
        self.framework.observe(self.on.get_suggestion_action, self._on_action_get_suggestion)
        self.framework.observe(self.on.show_status_action, self._on_action_show_status)
        self.framework.observe(self.on.show_usage_action, self._on_action_show_usage)
        self.framework.observe(self.on.reset_action, self._on_action_reset)

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _on_config_changed(self, event):
        """Validate config, then surface a broken controller prerequisite.

        Credentials are only required once the operator opts in to watching
        co-located units, so the zero-config path is never blocked. Mirrors
        the k8s charm, where the same check is unconditional because that
        charm always needs the controller.
        """
        super()._on_config_changed(event)
        if isinstance(self.unit.status, BlockedStatus):
            return
        prereq = self._prerequisite_error()
        if prereq:
            self.unit.status = BlockedStatus(prereq)

    def _on_update_status(self, event):
        try:
            relations = list(self.model.relations.get("principal", []))
        except Exception:
            relations = []

        if not relations:
            self.unit.status = MaintenanceStatus("waiting for principal relation")
            return

        # The principal is always monitored, and always from the local
        # goal-state hook tool: it needs no credentials and keeps working
        # when the controller is unreachable.
        self._log_principal_status()

        # Co-located units are opt-in and read from the controller API.
        # Empty watch-applications means no controller connection at all.
        if not self._watch_applications():
            return

        prereq = self._prerequisite_error()
        if prereq:
            self.unit.status = BlockedStatus(prereq)
            return

        try:
            statuses, other_jaime = self._fetch_co_located_statuses()
        except ControllerAuthError as e:
            logger.error("Juju controller authentication failed: %s", e)
            self.unit.status = BlockedStatus(
                "juju-api credentials rejected by controller"
            )
            return
        except ControllerError as e:
            logger.warning("could not fetch co-located unit statuses: %s", e)
            self.unit.status = MaintenanceStatus(str(e)[:100])
            return

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for unit_name, info in statuses.items():
            self._process_unit(unit_name, info["status"], info.get("since") or now_iso)

        self._report_other_jaime_units(other_jaime)

    def _watch_applications(self) -> list[str]:
        """Applications named in watch-applications, in config order."""
        raw = self.model.config.get("watch-applications", "")
        return [a.strip() for a in raw.split(",") if a.strip()]

    def _resolve_juju_password(self) -> str:
        """Resolve the Juju API password from config (plain or secret URI)."""
        return self._resolve_secret(
            self.model.config.get("juju-api-password", ""), "password"
        )

    def _machine_id(self) -> str | None:
        """This unit's machine id (JUJU_MACHINE_ID), via the ops JujuContext.

        Returns None when the full hook environment is absent (for example in
        unit tests). Callers treat that as a hard failure rather than skipping
        host filtering, so a unit with no host identity is never monitored.
        """
        try:
            return JujuContext.from_environ().machine_id
        except ValueError:
            return None

    def _prerequisite_error(self) -> str | None:
        """Return a blocked-status message while the controller is unusable.

        Only applies when the operator has opted in to watching co-located
        units, since the default path needs no controller. Credentials are
        checked first, then whether the controller accepts them. A missing
        agent.conf or an unreachable controller is treated as transient and
        left to the monitoring path, matching the k8s charm.
        """
        if not self._watch_applications():
            return None

        username = self.model.config.get("juju-api-user", "")
        password = self._resolve_juju_password()
        if not username or not password:
            return "juju-api-user and juju-api-password must be configured"

        conf_path = agent_conf_path(unit_name=self.unit.name)
        if conf_path is None:
            return None
        try:
            conf = parse_agent_conf(conf_path)
            with JujuControllerClient(
                conf["api_address"], conf["ca_cert"], conf["model_uuid"]
            ) as client:
                client.login(username, password)
        except ControllerAuthError:
            return "juju-api credentials rejected by controller"
        except Exception as e:
            logger.debug("controller prerequisite check failed: %s", e)
            return None
        return None

    def _fetch_co_located_statuses(self) -> tuple[dict[str, dict], list[str]]:
        """Fetch statuses of co-located units, and any other Jaime units.

        Returns ``(statuses, other_jaime)``. ``statuses`` is filtered to this
        unit's machine and excludes Jaime itself; the principal is excluded
        too because it is already handled by ``_log_principal_status``, and
        processing it twice per cycle would corrupt its incident counters.
        """
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

        machine = self._machine_id()
        if not machine:
            # Fail closed: without a host identity we cannot guarantee the
            # host-only boundary, and reporting a remote unit with this
            # machine's evidence is worse than reporting nothing.
            raise ControllerError("could not determine this unit's machine id")
        other_jaime = self._other_jaime_units(full, machine)

        watch = self._watch_applications()
        # "*" means every co-located unit. The shared extractor already treats
        # an empty watch list as "all applications"; k8s never passes empty,
        # so reusing that here does not change the k8s charm.
        watch_filter = [] if "*" in watch else watch

        exclude = [self.app.name]
        principal_app = self._get_principal_name()
        if principal_app:
            exclude.append(principal_app)

        statuses = extract_unit_statuses(
            full, watch_applications=watch_filter, exclude_applications=exclude
        )
        statuses = {
            name: info for name, info in statuses.items()
            if info.get("machine") == machine
        }
        return statuses, other_jaime

    def _other_jaime_units(self, full_status: dict, machine: str | None) -> list[str]:
        """Other Jaime units co-located on this machine, if any.

        They can only be seen through the controller, so this is empty on the
        credential-free default path. Two Jaime units on one host would open
        duplicate incidents for the same fault; deduplicating needs the peer
        relation and is deferred to Phase 6.1, so for now the case is detected
        and reported.
        """
        if not machine:
            return []
        all_units = extract_unit_statuses(full_status)
        return sorted(
            name for name, info in all_units.items()
            if name != self.unit.name
            and name.split("/")[0] == self.app.name
            and info.get("machine") == machine
        )

    def _report_other_jaime_units(self, other_jaime: list[str]) -> None:
        if not other_jaime:
            return
        logger.warning("other Jaime units co-located on this machine: %s", other_jaime)
        if isinstance(self.unit.status, ActiveStatus) and self.unit.status.message == "Ready":
            self.unit.status = ActiveStatus(
                f"Ready; {len(other_jaime)} other Jaime unit(s) on this machine "
                f"({', '.join(other_jaime)}); duplicate reports possible "
                "(deduplication deferred to Phase 6.1)"
            )

    def _log_principal_status(self):
        """Read principal unit workload status via goal-state and drive incidents."""
        # Build the set of units actually related to this Jaime instance.
        # This filter is load-bearing, not defensive: goal-state returns the
        # same content under both the 'principal' and 'juju-info' endpoints,
        # so without it a co-located sibling subordinate would be read as if
        # it were this unit's principal.
        own_principal_units: set[str] = set()
        for rel in self.model.relations.get("principal", []):
            for unit in rel.units:
                own_principal_units.add(unit.name)

        try:
            gs = goal_state()
            principal_relations = gs.relations.get("principal", {})
            for unit_name, goal in principal_relations.items():
                if "/" not in unit_name:
                    continue
                if own_principal_units and unit_name not in own_principal_units:
                    continue

                status = goal.status
                since_iso = goal.since.isoformat()
                self._process_unit(unit_name, status, since_iso)
        except Exception as e:
            logger.warning("could not read principal goal-state: %s", e)

    # ------------------------------------------------------------------
    # Substrate hooks used by CoreMixin
    # ------------------------------------------------------------------

    def _collect_incident_context(self, unit_name: str, since_iso: str,
                                  incident: Incident) -> dict:
        """Collect local diagnostic context on the machine (plan-driven)."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        try:
            from_dt = datetime.datetime.fromisoformat(since_iso)
        except ValueError:
            from_dt = None
        diagnostics_plan = read_diagnostics_file(self._diagnostics_path)

        context = collect_context(
            unit_name, log_window, max_lines,
            from_time=from_dt,
            diagnostics_plan=diagnostics_plan,
        )
        write_event({
            "event": "context-collected",
            "unit": unit_name,
            "incident_id": incident.id,
            "log_lines": len(context.get("unit_logs", [])),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, self.model.config.get("audit-log-path", ""))
        return context

    def _collect_report_context(self, unit_name: str, since_iso: str) -> dict:
        """Collect context for a manually regenerated report."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        diagnostics_plan = read_diagnostics_file(self._diagnostics_path)
        return collect_context(
            unit_name, log_window, max_lines,
            diagnostics_plan=diagnostics_plan,
        )

    # ------------------------------------------------------------------
    # Diagnostics plan
    # ------------------------------------------------------------------

    def _on_principal_joined(self, event):
        logger.info("principal relation joined: %s", event.relation)
        self._ensure_diagnostics()

    def _on_principal_changed(self, event):
        logger.info("principal relation changed: %s", event.relation)

    def _on_principal_broken(self, event):
        logger.info("principal relation broken: %s", event.relation)
        self.unit.status = MaintenanceStatus("principal relation removed")

    def _ensure_diagnostics(self):
        diagnostics_raw = self.model.config.get("diagnostics", "")

        if diagnostics_raw:
            self._apply_diagnostics_config(diagnostics_raw)
        else:
            self._generate_diagnostics()

    def _apply_diagnostics_config(self, diagnostics_raw):
        try:
            plan = json.loads(diagnostics_raw)
        except json.JSONDecodeError as e:
            logger.error("diagnostics config is not valid JSON: %s", e)
            self.unit.status = BlockedStatus("invalid diagnostics config (not JSON)")
            return

        errors = validate_diagnostics(plan)
        if errors:
            logger.error("diagnostics config validation failed: %s", errors)
            self.unit.status = BlockedStatus(f"invalid diagnostics config: {errors[0]}")
            return

        write_diagnostics_file(plan, self._diagnostics_path)
        logger.info("monitoring plan written to %s", self._diagnostics_path)
        self.unit.status = ActiveStatus("Ready")

    def _generate_diagnostics(self):
        principal_name = self._get_principal_name()
        if not principal_name:
            logger.warning("no principal name available, skipping diagnostics generation")
            self.unit.status = ActiveStatus("no principal to diagnose")
            return

        provider, _ = self._get_ai_provider()
        if provider is None:
            logger.info("no AI provider configured, writing empty monitoring plan")
            plan = make_empty_plan(principal_name)
            write_diagnostics_file(plan, self._diagnostics_path)
            self.unit.status = ActiveStatus("Ready")
            return

        logger.info("generating diagnostics plan for '%s'", principal_name)
        try:
            prompt = build_prompt(principal_name)
            response, _ = provider.generate(prompt)
            logger.info("Diagnostics plan generated successfully")
            logger.debug("Diagnostics plan AI response:\n%s", response)

            # Gemini may wrap the JSON in markdown fences despite being asked not to.
            stripped = response.strip()
            if stripped.startswith("```"):
                lines = stripped.splitlines()
                inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
                stripped = inner.strip()

            if not stripped:
                raise ValueError("AI provider returned an empty response")

            plan = json.loads(stripped)
            plan["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        except Exception as e:
            logger.error("Diagnostics plan generation failed: %s — falling back to empty plan", e)
            plan = make_empty_plan(principal_name)
            write_diagnostics_file(plan, self._diagnostics_path)
            self.unit.status = ActiveStatus("Ready")
            return

        errors = validate_diagnostics(plan)
        if errors:
            logger.error("AI generated invalid monitoring plan: %s — falling back to empty plan", errors)
            plan = make_empty_plan(principal_name)
            write_diagnostics_file(plan, self._diagnostics_path)
            self.unit.status = ActiveStatus("Ready")
            return

        write_diagnostics_file(plan, self._diagnostics_path)
        logger.info("AI-generated monitoring plan written to %s", self._diagnostics_path)
        self.unit.status = ActiveStatus("Ready")

    def _get_principal_name(self):
        try:
            rels = self.model.relations.get("principal", [])
            if rels:
                return rels[0].app.name
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Actions (machine-specific)
    # ------------------------------------------------------------------

    def _on_action_diagnose(self, event):
        logger.info("diagnose action invoked")
        principal = None
        try:
            rels = self.model.relations.get("principal") or []
            if rels:
                rel = rels[0]
                principal = list(rel.units)[0].name if list(rel.units) else None
        except Exception:
            principal = None

        result = {
            "principal-unit": principal or "unknown",
            "jaime-unit": self.unit.name,
            "jaime-mode": self.model.config.get("mode"),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        event.set_results(result)

    def _on_action_collect_context(self, event):
        logger.info("collect-context action invoked")
        event.set_results({"context-path": "/var/lib/jaime/incidents/placeholder-context.json"})


if __name__ == "__main__":
    main(JaimeCharm)
