#!/usr/bin/env python3
"""Jaime charm — diagnostics plan generation on relation-joined."""

import datetime
import json
import logging
import os

from ops import JujuContext
from ops.charm import CharmBase
from ops.hookcmds import goal_state
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, ModelError

from jaime.collector import collect_context
from jaime.controller import (
    ControllerAuthError,
    ControllerError,
    extract_app_metadata,
    extract_unit_statuses,
)
from jaime.core import CREDENTIALS_REJECTED, CREDENTIALS_REQUIRED, CoreMixin
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
    _context_dir = "/var/lib/jaime/incidents"

    def __init__(self, *args):
        super().__init__(*args)
        self._status_tracker = StatusTracker()

        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.principal_relation_changed, self._on_principal_changed)
        self.framework.observe(self.on.principal_relation_joined, self._on_principal_joined)
        self.framework.observe(self.on.principal_relation_broken, self._on_principal_broken)

        self.framework.observe(self.on.collect_context_action, self._on_action_collect_context)
        self.framework.observe(self.on.generate_report_action, self._on_action_generate_report)
        self.framework.observe(self.on.get_suggestion_action, self._on_action_get_suggestion)
        self.framework.observe(self.on.ask_action, self._on_action_ask)
        self.framework.observe(self.on.list_suggestions_action, self._on_action_list_suggestions)
        self.framework.observe(self.on.show_status_action, self._on_action_show_status)
        self.framework.observe(self.on.show_usage_action, self._on_action_show_usage)
        self.framework.observe(self.on.list_incidents_action, self._on_action_list_incidents)
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
        relations = self._principal_relations()

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
            self.unit.status = BlockedStatus("juju-api credentials rejected by controller")
            return
        except ControllerError as e:
            logger.warning("could not fetch co-located unit statuses: %s", e)
            self.unit.status = MaintenanceStatus(str(e)[:100])
            return

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for unit_name, info in statuses.items():
            self._process_unit(
                unit_name,
                info["status"],
                info.get("since") or now_iso,
                info.get("message", ""),
            )

        self._report_other_jaime_units(other_jaime)

    def _watch_applications(self) -> list[str]:
        """Applications named in watch-applications, in config order."""
        raw = self.model.config.get("watch-applications", "")
        return [a.strip() for a in raw.split(",") if a.strip()]

    def _monitored_applications(self) -> list[str]:
        """Applications resolved as monitored, derived from persisted state.

        The principal is always monitored. Co-located applications are the ones
        with at least one observed unit in the tracker, intersected with the
        current watch-applications (``*`` means every co-located unit).
        Deriving from the tracker rather than an in-memory per-hook cache keeps
        the answer correct in every hook, not only inside a monitoring cycle.
        """
        watch = self._watch_applications()
        wildcard = "*" in watch
        tracked = {name.split("/")[0] for name in self._status_tracker.units()}
        apps = []
        principal = self._get_principal_name()
        if principal:
            apps.append(principal)
        for app in sorted(tracked):
            if app == principal:
                continue
            if wildcard or app in watch:
                apps.append(app)
        return apps

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

        if not self._has_controller_credentials():
            return CREDENTIALS_REQUIRED

        try:
            with self._controller_session():
                pass
        except ControllerAuthError:
            return CREDENTIALS_REJECTED
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
        with self._controller_session() as client:
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

        statuses = extract_unit_statuses(full, watch_applications=watch_filter, exclude_applications=exclude)
        statuses = {name: info for name, info in statuses.items() if info.get("machine") == machine}
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
            name
            for name, info in all_units.items()
            if name != self.unit.name and name.split("/")[0] == self.app.name and info.get("machine") == machine
        )

    def _report_other_jaime_units(self, other_jaime: list[str]) -> None:
        if not other_jaime:
            return
        logger.warning("other Jaime units co-located on this machine: %s", other_jaime)
        if isinstance(self.unit.status, ActiveStatus) and self.unit.status.message.startswith("Ready"):
            self.unit.status = ActiveStatus(
                f"{self._ready_message()}; {len(other_jaime)} other Jaime unit(s) "
                f"on this machine ({', '.join(other_jaime)}); duplicate reports "
                "possible (deduplication deferred to Phase 6.1)"
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

        watched = self._watch_statuses()
        observations: list[tuple[str, str, str]] = []
        try:
            gs = goal_state()
            principal_relations = gs.relations.get("principal", {})
            for unit_name, goal in principal_relations.items():
                if "/" not in unit_name:
                    continue
                if own_principal_units and unit_name not in own_principal_units:
                    continue
                status = goal.status
                observations.append((unit_name, status, goal.since.isoformat()))
        except Exception as e:
            logger.warning("could not read principal goal-state: %s", e)
            return

        # goal-state carries only the status name and timestamp, not the
        # free-text message shown by `juju status`. When the operator has
        # configured controller credentials, enrich watched observations with
        # that message so it reaches the report. Connected only when a watched
        # status is present, so a healthy principal never opens a controller
        # connection.
        messages = {}
        if any(status in watched for _, status, _ in observations):
            messages = self._principal_status_messages()

        for unit_name, status, since_iso in observations:
            self._process_unit(unit_name, status, since_iso, messages.get(unit_name, ""))

    def _principal_status_messages(self) -> dict[str, str]:
        """Workload status messages for the principal unit, keyed by unit name.

        The principal is monitored from goal-state, which has no message. When
        controller credentials are configured, the message is read from
        ``Client.FullStatus`` so the report can carry the reason Juju shows
        (e.g. "Please initialize OpenBao or integrate with an auto-unseal
        provider"). Returns an empty mapping — leaving the goal-state path
        unchanged — when credentials are absent, the principal cannot be
        resolved, or the controller is unreachable.
        """
        principal_app = self._get_principal_name()
        if not principal_app:
            return {}

        def messages(client):
            statuses = extract_unit_statuses(client.full_status(), watch_applications=[principal_app])
            return {name: info.get("message", "") for name, info in statuses.items() if info.get("message")}

        return self._controller_query(messages, {}, "principal status messages")

    # ------------------------------------------------------------------
    # Substrate hooks used by CoreMixin
    # ------------------------------------------------------------------

    def _collect_incident_context(self, unit_name: str, since_iso: str, incident: Incident) -> dict:
        """Collect local diagnostic context on the machine (plan-driven)."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        try:
            from_dt = datetime.datetime.fromisoformat(since_iso)
        except ValueError:
            from_dt = None
        diagnostics_plan = read_diagnostics_file(self._diagnostics_path)

        context = collect_context(
            unit_name,
            log_window,
            max_lines,
            from_time=from_dt,
            diagnostics_plan=diagnostics_plan,
        )
        write_event(
            {
                "event": "context-collected",
                "unit": unit_name,
                "incident_id": incident.id,
                "log_lines": len(context.get("unit_logs", [])),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            self.model.config.get("audit-log-path", ""),
        )
        self._add_principal_controller_details(context)
        return context

    def _collect_report_context(self, unit_name: str, since_iso: str) -> dict:
        """Collect context for a manually regenerated report."""
        log_window = self.model.config.get("log-window-minutes", 30)
        max_lines = self.model.config.get("max-context-lines", 500)
        diagnostics_plan = read_diagnostics_file(self._diagnostics_path)
        context = collect_context(
            unit_name,
            log_window,
            max_lines,
            diagnostics_plan=diagnostics_plan,
        )
        self._add_principal_controller_details(context)
        return context

    def _add_principal_controller_details(self, context: dict) -> None:
        """Add the principal's charm details and live config to a report context.

        Best-effort: both are left out (and the report omits those parts) when
        the principal cannot be resolved or the operator has not configured
        controller credentials. The principal's status is read from goal-state,
        which carries no channel, revision or config values, so the controller
        is the only source for them.
        """
        principal_app = self._get_principal_name()
        if not principal_app:
            return
        context["app_metadata"] = self._controller_query(
            lambda client: extract_app_metadata(client.full_status(), principal_app),
            {},
            "principal app metadata",
        )
        context["juju_config"] = self._controller_query(
            lambda client: client.application_get(principal_app).get("config", {}),
            {},
            "principal config",
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

    def _principal_relations(self) -> list:
        """The ``principal`` relations, or none when the model cannot be read."""
        try:
            return list(self.model.relations.get("principal", []))
        except ModelError as e:
            logger.debug("could not read the principal relation: %s", e)
            return []

    def _get_principal_name(self):
        relations = self._principal_relations()
        return relations[0].app.name if relations else None

    # ------------------------------------------------------------------
    # Actions (machine-specific)
    # ------------------------------------------------------------------

    def _principal_unit_name(self):
        """Name of the first related principal unit, or None."""
        for rel in self._principal_relations():
            units = list(rel.units)
            if units:
                return units[0].name
        return None

    def _on_action_collect_context(self, event):
        """Collect a bounded context bundle for the principal and write it to disk."""
        logger.info("collect-context action invoked")
        unit = self._principal_unit_name()
        if not unit:
            event.fail("no principal unit related")
            return

        context = self._collect_report_context(unit, "")
        path = os.path.join(self._context_dir, f"{unit.replace('/', '-')}-context.json")
        try:
            os.makedirs(self._context_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(context, f, indent=2, default=str)
        except Exception as e:
            event.fail(f"could not write context bundle: {e}")
            return
        event.set_results({"unit": unit, "context-path": path})


if __name__ == "__main__":
    main(JaimeCharm)
