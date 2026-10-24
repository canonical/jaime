"""Shared charm logic for both the machine and k8s Jaime charms.

This module contains the substrate-agnostic behaviour that both charms share:
mode/provider enums, AI provider wiring, the suggest/act engine invocation,
usage tracking, audit logging, action handlers, and the per-unit incident
lifecycle state machine. Each concrete charm subclasses ``CoreMixin`` and
supplies only the substrate-specific hooks (how units are discovered and how
context is collected).

Note: this module is the charm controller layer and deliberately imports the
ops status classes; it is shared between the two charms but is not part of
the substrate-agnostic pure library.
"""

import contextlib
import datetime
import hashlib
import json
import logging
import traceback

from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from jaime.actions import ActionsMixin
from jaime.check_cache import CheckCache
from jaime.controller import (
    ControllerError,
    JujuControllerClient,
    agent_conf_path,
    parse_agent_conf,
)
from jaime.incident import Incident, Suggestion, Turn
from jaime.logging import write_event
from jaime.modes import Mode, Provider
from jaime.report import generate_report
from jaime.suggest import run_act, run_ask, run_suggest

logger = logging.getLogger(__name__)

# Blocked-status messages for the observer credentials, shared by both charms.
CREDENTIALS_REQUIRED = "juju-api-user and juju-api-password must be configured"
CREDENTIALS_REJECTED = "juju-api credentials rejected by controller"


class CoreMixin(ActionsMixin):
    """Substrate-agnostic behaviour shared by both Jaime charms.

    Concrete charms must inherit from ``CoreMixin`` and ``CharmBase``, create
    ``self._status_tracker`` in ``__init__``, observe events, and implement the
    substrate hooks:

    - ``_collect_incident_context(unit, since_iso, incident)`` -> context dict
    - ``_collect_report_context(unit, since_iso)`` -> context dict
    """

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _on_config_changed(self, event):
        """Validate config and AI provider connectivity on config change."""
        try:
            mode = Mode(self.model.config.get("mode", Mode.OBSERVE))
        except ValueError:
            valid = ", ".join(m.value for m in Mode)
            self.unit.status = BlockedStatus(f"invalid mode '{self.model.config.get('mode')}', must be one of: {valid}")
            return

        try:
            Provider(self.model.config.get("provider", Provider.NONE))
        except ValueError:
            valid = ", ".join(p.value for p in Provider)
            self.unit.status = BlockedStatus(
                f"invalid provider '{self.model.config.get('provider')}', must be one of: {valid}"
            )
            return

        # provider-remote-url has exactly one consumer, the remote provider.
        # Set alongside gemini or openrouter it is a misconfiguration, so
        # surface it rather than letting it pass unnoticed. With provider=none
        # it is dead config (no provider is used), so it is left alone.
        remote_url = (self.model.config.get("provider-remote-url", "") or "").strip()
        if remote_url and Provider(self.model.config.get("provider", Provider.NONE)) in (
            Provider.GEMINI,
            Provider.OPENROUTER,
        ):
            self.unit.status = BlockedStatus("provider-remote-url is set but provider is not 'remote'")
            return

        # Act mode is gated until command allowlisting is implemented.
        if mode == Mode.ACT:
            self.unit.status = BlockedStatus("act mode is not yet implemented")
            return

        provider, provider_err = self._get_ai_provider()
        if provider is None and mode in (Mode.SUGGEST, Mode.ACT):
            self.unit.status = BlockedStatus(provider_err)
            return

        if provider is not None:
            err = provider.check()
            if err:
                logger.error("AI provider connectivity check failed: %s", err)
                self.unit.status = BlockedStatus(f"AI provider error: {err[:100]}")
                return

        self._refresh_status_after_config()

    def _refresh_status_after_config(self) -> None:
        """Reconcile the display status after a config change.

        A config-changed event must not overwrite an open-incident status with
        a plain "Ready". If any unit still has an open incident, surface that;
        otherwise fall back to Active("Ready").
        """
        for unit_name, entry, inc in self._status_tracker.incidents():
            short_id = inc.get("id", "")[:8]
            self.unit.status = ActiveStatus(f"incident open: {unit_name} {entry.get('status', 'unknown')} ({short_id})")
            return
        self.unit.status = ActiveStatus(self._ready_message())

    def _monitored_applications(self) -> list[str]:
        """Applications resolved as monitored this cycle, substrate-specific.

        The default is empty, so a substrate that does not resolve a set (or a
        test that does not populate one) reports a plain ``Ready``. Concrete
        charms override this and cache the set for the cycle.
        """
        return []

    def _ready_message(self) -> str:
        """Active-status text, naming the monitored applications when there are any."""
        apps = self._monitored_applications()
        if not apps:
            return "Ready"
        return "Ready: monitoring " + ", ".join(apps)

    def _watch_statuses(self) -> set[str]:
        return {s.strip() for s in self.model.config.get("watch-statuses", "error,blocked").split(",") if s.strip()}

    # ------------------------------------------------------------------
    # AI provider
    # ------------------------------------------------------------------

    def _get_ai_provider(self):
        try:
            provider_name = Provider(self.model.config.get("provider", Provider.NONE))
        except ValueError:
            return None, f"unsupported provider '{self.model.config.get('provider')}'"

        if provider_name == Provider.NONE:
            return None, (f"mode={self.model.config.get('mode')} but provider is not configured")

        model = self.model.config.get("model", "") or self._default_model(provider_name.value)

        # The remote (OpenAI-compatible) provider has no fixed endpoint and no
        # provider default model. It also needs no token: local servers such as
        # Ollama accept unauthenticated calls, so no Authorization header is
        # sent when api-token is empty.
        if provider_name == Provider.REMOTE:
            base_url = (self.model.config.get("provider-remote-url", "") or "").strip()
            if not base_url:
                return None, "provider=remote but provider-remote-url is not set"
            if not model:
                return None, "provider=remote but model is not set"
            api_token = self._resolve_api_token()
            from jaime.providers.remote import RemoteProvider

            return RemoteProvider(api_token, model, base_url), None

        api_token = self._resolve_api_token()
        if not api_token:
            logger.warning("provider '%s' configured but api-token is empty", provider_name.value)
            return None, f"provider={provider_name.value} but api-token is not set"

        if provider_name == Provider.GEMINI:
            from jaime.providers.gemini import GeminiProvider

            return GeminiProvider(api_token, model), None
        elif provider_name == Provider.OPENROUTER:
            from jaime.providers.openrouter import OpenRouterProvider

            return OpenRouterProvider(api_token, model), None

        return None, f"unsupported provider '{provider_name.value}'"

    def _log_event(self, level: int, event: str, now: str, **fields) -> None:
        """Log a structured JSON event (the unit log, not the audit file)."""
        logger.log(level, json.dumps({"event": event, **fields, "timestamp": now}))

    def _audit(self, event: str, now: str, **fields) -> None:
        """Append an event to the JSONL audit log."""
        write_event({"event": event, **fields, "timestamp": now}, self.model.config.get("audit-log-path", ""))

    def _resolve_juju_password(self) -> str:
        """Resolve the Juju API password from config (plain or secret URI)."""
        return self._resolve_secret(self.model.config.get("juju-api-password", ""), "password")

    def _has_controller_credentials(self) -> bool:
        return bool(self.model.config.get("juju-api-user", "") and self._resolve_juju_password())

    @contextlib.contextmanager
    def _controller_session(self):
        """Yield a Juju controller client logged in as the observer user.

        Raises ``ControllerError`` when the credentials are not configured or
        the controller cannot be located, and ``ControllerAuthError`` when the
        controller rejects them.
        """
        if not self._has_controller_credentials():
            raise ControllerError(CREDENTIALS_REQUIRED)

        conf_path = agent_conf_path(unit_name=self.unit.name)
        if conf_path is None:
            raise ControllerError("agent.conf not found: cannot locate controller")

        conf = parse_agent_conf(conf_path)
        with JujuControllerClient(conf["api_address"], conf["ca_cert"], conf["model_uuid"]) as client:
            client.login(self.model.config.get("juju-api-user", ""), self._resolve_juju_password())
            yield client

    def _controller_query(self, query, default, what: str):
        """Run ``query(client)`` against the controller, best effort.

        Returns ``default`` (and logs why at debug level) when the controller
        is not configured, unreachable, or the query fails: report enrichment
        must never break the incident flow.
        """
        try:
            with self._controller_session() as client:
                return query(client)
        except Exception as e:
            logger.debug("could not fetch %s: %s", what, e)
            return default

    def _resolve_api_token(self) -> str:
        """Resolve the api-token config value (secret URI or plain string)."""
        return self._resolve_secret(self.model.config.get("api-token", ""), "token")

    def _resolve_secret(self, raw: str, field: str) -> str:
        """Resolve a config value that may be a Juju secret URI (``secret:<id>``)."""
        if not raw:
            return ""
        if raw.startswith("secret:"):
            try:
                secret = self.model.get_secret(id=raw)
                return secret.get_content(refresh=True).get(field, "")
            except Exception as e:
                logger.warning("could not retrieve secret: %s", e)
                return ""
        return raw

    @staticmethod
    def _default_model(provider_name):
        mapping = {"gemini": "gemini-2.5-flash", "openrouter": "~deepseek/deepseek-v4-flash-latest"}
        return mapping.get(provider_name, "")

    # ------------------------------------------------------------------
    # Suggest / act
    # ------------------------------------------------------------------

    def _run_mode_logic(
        self,
        report_content: str,
        additional_context: str = "",
        question: str = "",
        history: list[dict] | None = None,
        block_on_error: bool = True,
    ) -> Suggestion | None:
        """Run suggest or act logic based on the configured mode.

        A non-empty ``question`` answers it as a conversational turn, with
        ``history`` carrying the prior turns; act mode ignores both. On
        failure the reason is left in ``self._last_ai_error``, and the unit is
        set to blocked unless ``block_on_error`` is false (``ask`` reports the
        error to the caller instead, since a bot may retry it often).
        """
        self._last_ai_error = ""

        def fail(message: str) -> None:
            self._last_ai_error = message
            if block_on_error:
                self.unit.status = BlockedStatus(message)

        try:
            mode = Mode(self.model.config.get("mode", Mode.OBSERVE))
        except ValueError:
            return None
        if mode not in (Mode.SUGGEST, Mode.ACT):
            return None
        if mode == Mode.ACT:
            logger.warning("act mode is not yet implemented — ignoring")
            return None

        provider, provider_err = self._get_ai_provider()
        if provider is None:
            logger.warning("AI provider unavailable: %s", provider_err)
            fail(provider_err)
            return None

        err = self._check_provider(provider)
        if err:
            logger.error("AI provider token check failed before generation: %s", err)
            fail(f"AI provider error: {err[:100]}")
            return None

        try:
            if mode == Mode.SUGGEST:
                if question:
                    return run_ask(provider, report_content, question, history)
                return run_suggest(provider, report_content, additional_context)

            suggestion, act_results = run_act(provider, report_content, additional_context)
            for result in act_results:
                write_event(
                    {
                        "event": "act-command-executed",
                        "command": result["command"],
                        "returncode": result["returncode"],
                        "stderr": result.get("stderr", ""),
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    },
                    self.model.config.get("audit-log-path", ""),
                )
            return suggestion

        except Exception as e:
            logger.error("AI provider call failed in mode '%s': %s", mode.value, traceback.format_exc())
            fail(f"AI provider error: {str(e)[:100]}")
            return None

    def _check_provider(self, provider) -> str | None:
        """Connectivity check, skipped while a recent success is on record.

        A chat bot may call ``ask`` many times a minute; checking before every
        call would add a round trip and a failure point to each of them.
        """
        cache = CheckCache(self._status_tracker.path + ".provider-check")
        key = hashlib.sha256(
            "\0".join(
                [
                    str(self.model.config.get("provider", "")),
                    str(self.model.config.get("model", "")),
                    str(self.model.config.get("provider-remote-url", "")),
                    self._resolve_api_token(),
                ]
            ).encode()
        ).hexdigest()
        now = datetime.datetime.now(datetime.timezone.utc)
        if cache.is_fresh(key, now):
            return None
        err = provider.check()
        if err is None:
            cache.record(key, now)
        return err

    def _store_suggestion(self, unit_name: str, incident_dict: dict, suggestion: Suggestion) -> Suggestion:
        """Append a suggestion as a turn (legacy one-shot), persist, audit."""
        turn = Turn.from_suggestion(suggestion)
        self._store_turn(unit_name, incident_dict, turn)
        return suggestion

    def _store_turn(self, unit_name: str, incident_dict: dict, turn: Turn, history_count: int = 0) -> Turn:
        """Append a conversational turn to the incident, prune, persist, audit.

        Returns the persisted turn, carrying its assigned per-incident id.
        """
        updated = Incident.from_dict(incident_dict).append_turn(turn)
        turn = updated.turns[-1]
        updated = updated.prune_turns()
        self._status_tracker.update_incident(unit_name, updated.to_dict())

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        extra = {"usage": turn.usage.to_dict()} if turn.usage is not None else {}
        self._audit(
            "suggestion-generated",
            now,
            unit=unit_name,
            incident_id=incident_dict["id"],
            turn_id=turn.id,
            question=turn.question,
            history_turns=history_count,
            commands=list(turn.commands),
            mode=self.model.config.get("mode", Mode.OBSERVE.value),
            **extra,
        )

        if turn.usage is not None:
            usage_entry = turn.usage.to_dict()
            usage_entry["incident_id"] = incident_dict["id"]
            usage_entry["turn_id"] = turn.id
            usage_entry["timestamp"] = now
            self._status_tracker.record_usage(unit_name, usage_entry)

        return turn

    # ------------------------------------------------------------------
    # Incident lifecycle (shared state machine)
    # ------------------------------------------------------------------

    def _process_unit(self, unit_name: str, status: str, since_iso: str, message: str = "") -> None:
        """Drive the incident lifecycle for a single observed unit.

        ``message`` is the workload status message shown by ``juju status``
        (e.g. "Please initialize OpenBao or integrate with an auto-unseal
        provider"). Empty when the observing path cannot see it — the machine
        charm's goal-state hook tool carries no message.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.isoformat()
        watch_statuses = self._watch_statuses()
        failure_timeout = self.model.config.get("failure-timeout-minutes", 5)
        cooldown = self.model.config.get("cooldown-minutes", 30)

        watched = status in watch_statuses
        had_open_incident = self._status_tracker.has_open_incident(unit_name)
        prior_incident = self._status_tracker.current_incident(unit_name)
        increment = self._status_tracker.observe(unit_name, status, since_iso, watched, message)

        # --- Recovery ---
        if not watched:
            if increment == 1 and had_open_incident and prior_incident:
                closed = Incident.from_dict(prior_incident).close()
                self._status_tracker.close_incident(unit_name, closed.to_dict())
                self._log_event(
                    logging.INFO, "incident-closed", ts, unit=unit_name, workload=status, incident=closed.to_dict()
                )
                self._audit(
                    "incident-closed",
                    ts,
                    unit=unit_name,
                    workload=status,
                    incident_id=closed.id,
                    closed_at=closed.closed_at,
                )
            elif increment == 1:
                self._log_event(logging.DEBUG, "principal-status-recovered", ts, unit=unit_name, workload=status)
            else:
                logger.debug(
                    "unit %s: workload=%s (not watched, increment=%d)",
                    unit_name,
                    status,
                    increment,
                )
            self.unit.status = ActiveStatus(self._ready_message())
            return

        # Time the incident from when Jaime first saw this unit go unhealthy,
        # not from Juju's `since`. Juju bumps `since` every time a charm
        # re-sets its status — including re-setting the same status with a new
        # message, or flapping between two watched statuses — so a workload
        # stuck in a retry loop would otherwise reset the timer forever and
        # never reach failure-timeout-minutes.
        first_seen = self._status_tracker.unhealthy_since(unit_name) or since_iso

        self._log_event(
            logging.DEBUG,
            "principal-status-watched",
            ts,
            unit=unit_name,
            workload=status,
            first_seen=first_seen,
            status_since=since_iso,
            increment=increment,
        )

        # --- Within failure-timeout: waiting ---
        try:
            since_dt = datetime.datetime.fromisoformat(first_seen)
            unhealthy_minutes = (now - since_dt).total_seconds() / 60
        except ValueError:
            unhealthy_minutes = failure_timeout  # unknown since: treat as stale

        if unhealthy_minutes < failure_timeout:
            logger.debug(
                "unit %s: unhealthy for %.1f min, waiting for failure-timeout (%d min)",
                unit_name,
                unhealthy_minutes,
                failure_timeout,
            )
            self.unit.status = WaitingStatus(f"{status} - waiting ({unhealthy_minutes:.1f}/{failure_timeout} min)")
            return

        # --- Cooldown: incident already open ---
        last_reported_iso = self._status_tracker.last_reported(unit_name)
        if last_reported_iso:
            try:
                last_reported_dt = datetime.datetime.fromisoformat(last_reported_iso)
                cooldown_elapsed = (now - last_reported_dt).total_seconds() / 60
            except ValueError:
                cooldown_elapsed = cooldown
            if cooldown_elapsed < cooldown:
                incident_dict = self._status_tracker.current_incident(unit_name)
                short_id = (incident_dict or {}).get("id", "")[:8]
                self._log_event(
                    logging.DEBUG,
                    "principal-status-cooldown",
                    ts,
                    unit=unit_name,
                    workload=status,
                    first_seen=first_seen,
                    increment=increment,
                    cooldown_elapsed_minutes=round(cooldown_elapsed, 1),
                    cooldown_minutes=cooldown,
                    incident=incident_dict,
                )
                # Preserve BlockedStatus if a provider error was already set.
                if not isinstance(self.unit.status, BlockedStatus):
                    self.unit.status = ActiveStatus(f"incident open: {unit_name} {status} ({short_id})")
                return

        # --- Open a new incident ---
        incident = Incident.open(status_message=message)
        short_id = incident.id[:8]
        self._log_event(
            logging.INFO,
            "incident-opened",
            ts,
            unit=unit_name,
            workload=status,
            status_message=message,
            first_seen=first_seen,
            status_since=since_iso,
            increment=increment,
            incident=incident.to_dict(),
        )
        self._status_tracker.record_reported(unit_name, ts, incident.to_dict())
        self._audit(
            "incident-start",
            ts,
            unit=unit_name,
            workload=status,
            status_message=message,
            first_seen=first_seen,
            status_since=since_iso,
            incident_id=incident.id,
        )

        # Collect context and generate report via the substrate hook.
        self.unit.status = MaintenanceStatus(f"collecting context: {unit_name} ({short_id})")
        context = self._collect_incident_context(unit_name, first_seen, incident)

        report_path = generate_report(
            incident_id=incident.id,
            unit_name=unit_name,
            workload=status,
            first_seen=first_seen,
            context=context,
            report_dir=self.model.config.get("report-dir", ""),
            status_message=message,
        )
        self._audit("report-generated", ts, unit=unit_name, incident_id=incident.id, report_path=report_path)
        logger.info("incident %s: report written to %s", short_id, report_path)

        # Run suggest/act: produce a Suggestion and attach it to the incident.
        with open(report_path) as f:
            report_content = f.read()

        suggestion = self._run_mode_logic(report_content)
        if suggestion is not None:
            self._store_suggestion(unit_name, incident.to_dict(), suggestion)
        if not isinstance(self.unit.status, BlockedStatus):
            self.unit.status = ActiveStatus(f"incident open: {unit_name} {status} ({short_id})")

    # ------------------------------------------------------------------
    # Shared action handlers
    # ------------------------------------------------------------------
