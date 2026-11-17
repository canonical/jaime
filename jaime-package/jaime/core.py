"""Shared charm logic for both the machine and k8s Jaime charms.

This module contains the substrate-agnostic behaviour that both charms share:
mode/provider enums, AI provider wiring, the suggest engine invocation, usage
tracking, audit logging, action handlers, and the per-unit incident lifecycle
state machine. Each concrete charm subclasses ``CoreMixin`` and supplies only
the substrate-specific hooks (how units are discovered and how context is
collected).

Note: this module is the charm controller layer and deliberately imports the
ops status classes; it is shared between the two charms but is not part of
the substrate-agnostic pure library.
"""

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import traceback

from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, StatusBase, WaitingStatus

from jaime.actions import ActionsMixin
from jaime.check_cache import CheckCache
from jaime.controller import (
    ControllerAuthError,
    ControllerError,
    JujuControllerClient,
    agent_conf_path,
    parse_agent_conf,
)
from jaime.incident import Incident, Suggestion, Turn
from jaime.logging import write_event
from jaime.modes import Mode, Provider
from jaime.projection import PROJECTION_VERSION, build_projection
from jaime.report import generate_report
from jaime.suggest import run_ask, run_suggest
from jaime.timeutil import parse_ts, utc_now, utc_now_iso

logger = logging.getLogger(__name__)

# Blocked-status messages for the observer credentials, shared by both charms.
CREDENTIALS_REQUIRED = "juju-api-user and juju-api-password must be configured"
CREDENTIALS_REJECTED = "juju-api credentials rejected by controller"


class CoreMixin(ActionsMixin):
    """Substrate-agnostic behaviour shared by both Jaime charms.

    Concrete charms must inherit from ``CoreMixin`` and ``CharmBase``, create
    ``self._status_tracker`` in ``__init__``, observe events, and implement the
    substrate hook:

    - ``_collect_context(unit, from_time)`` -> context dict, where
      ``from_time`` is the aware datetime the incident window starts at, or
      None for a window ending now.

    Optional hooks, each returning a blocked-status message or None:
    ``_substrate_config_error()`` and ``_prerequisite_error()``.
    """

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _on_config_changed(self, event):
        """Validate config, then the substrate config and prerequisites.

        The first failing check blocks the unit. When all pass, the status is
        reconciled with any open incident.
        """
        with self._hook_cache():
            for check in (self._shared_config_error, self._substrate_config_error, self._prerequisite_error):
                error = check()
                if error:
                    self.unit.status = BlockedStatus(error)
                    return
            self._refresh_status_after_config()

    def _shared_config_error(self) -> str | None:
        """Validate the options shared by both charms, AI connectivity included."""
        mode = self._mode()
        if mode is None:
            valid = ", ".join(m.value for m in Mode)
            return f"invalid mode '{self.model.config.get('mode')}', must be one of: {valid}"

        try:
            provider_name = Provider(self.model.config.get("provider", Provider.NONE))
        except ValueError:
            valid = ", ".join(p.value for p in Provider)
            return f"invalid provider '{self.model.config.get('provider')}', must be one of: {valid}"

        # provider-remote-url has exactly one consumer, the remote provider.
        # Set alongside gemini or openrouter it is a misconfiguration, so
        # surface it rather than letting it pass unnoticed. With provider=none
        # it is dead config (no provider is used), so it is left alone.
        remote_url = (self.model.config.get("provider-remote-url", "") or "").strip()
        if remote_url and provider_name in (Provider.GEMINI, Provider.OPENROUTER):
            return "provider-remote-url is set but provider is not 'remote'"

        # Act mode is gated until command allowlisting is implemented.
        if mode == Mode.ACT:
            return "act mode is not yet implemented"

        provider, provider_err = self._get_ai_provider()
        if provider is None and mode == Mode.SUGGEST:
            return provider_err

        if provider is not None:
            err = provider.check()
            if err:
                logger.error("AI provider connectivity check failed: %s", err)
                return f"AI provider error: {err[:100]}"
        return None

    def _substrate_config_error(self) -> str | None:
        """Substrate-specific config check; None when there is nothing to report."""
        return None

    def _prerequisite_error(self) -> str | None:
        """Blocked-status message while a substrate prerequisite is unmet, or None."""
        return None

    def _mode(self) -> Mode | None:
        """The configured mode, or None when the option holds an invalid value."""
        try:
            return Mode(self.model.config.get("mode", Mode.OBSERVE))
        except ValueError:
            return None

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

    def _watch_applications(self) -> list[str]:
        """Applications named in watch-applications, in config order."""
        raw = self.model.config.get("watch-applications", "") or ""
        return [a.strip() for a in raw.split(",") if a.strip()]

    def _monitored_applications(self) -> list[str]:
        """Applications resolved as monitored this cycle, substrate-specific.

        The default is empty, so a substrate that does not resolve a set (or a
        test that does not populate one) reports a plain ``Ready``. Concrete
        charms override this.
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

    def _audit(self, event: str, now: str = "", **fields) -> None:
        """Append an event to the JSONL audit log (timestamped now by default)."""
        write_event(
            {"event": event, **fields, "timestamp": now or utc_now_iso()},
            self.model.config.get("audit-log-path", ""),
        )

    # ------------------------------------------------------------------
    # Juju controller access
    # ------------------------------------------------------------------

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

    @contextlib.contextmanager
    def _hook_cache(self):
        """Scope the per-hook controller data to one handler.

        Each hook is a fresh process, so this is a no-op in production; it
        keeps data from leaking between handlers that share an instance, as
        in unit tests.
        """
        self._full_status_result = None
        try:
            yield
        finally:
            self._full_status_result = None

    def _full_status(self) -> dict:
        """``Client.FullStatus`` for the model, fetched at most once per hook.

        Every caller in a hook shares one connection, login and fetch. A
        failure is remembered too, so an unreachable controller costs one
        timeout per hook rather than one per caller. Raises ``ControllerError``
        (``ControllerAuthError`` when the credentials are rejected).
        """
        cached = getattr(self, "_full_status_result", None)
        if cached is None:
            try:
                with self._controller_session() as client:
                    cached = (True, client.full_status())
            except ControllerError as e:
                cached = (False, e)
            except Exception as e:
                cached = (False, ControllerError(f"FullStatus failed: {e}"))
            self._full_status_result = cached
        ok, value = cached
        if not ok:
            raise value
        return value

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

    def _full_status_query(self, query, default, what: str):
        """Run ``query(full_status)`` on the per-hook FullStatus, best effort."""
        try:
            return query(self._full_status())
        except Exception as e:
            logger.debug("could not fetch %s: %s", what, e)
            return default

    @staticmethod
    def _controller_error_status(error: ControllerError) -> StatusBase:
        """Unit status for a controller error met while fetching statuses."""
        if isinstance(error, ControllerAuthError):
            logger.error("Juju controller authentication failed: %s", error)
            return BlockedStatus(CREDENTIALS_REJECTED)
        logger.warning("could not fetch unit statuses: %s", error)
        return MaintenanceStatus(str(error)[:100])

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

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
    # Suggest
    # ------------------------------------------------------------------

    def _run_mode_logic(
        self,
        report_content: str,
        additional_context: str = "",
        question: str = "",
        history: list[dict] | None = None,
        block_on_error: bool = True,
    ) -> Suggestion | None:
        """Run the suggest logic when the configured mode is ``suggest``.

        A non-empty ``question`` answers it as a conversational turn, with
        ``history`` carrying the prior turns. On failure the reason is left in
        ``self._last_ai_error``, and the unit is set to blocked unless
        ``block_on_error`` is false (``ask`` reports the error to the caller
        instead, since a bot may retry it often). Act mode is gated and never
        calls the provider.
        """
        self._last_ai_error = ""
        self._last_projection = None

        def fail(message: str) -> None:
            self._last_ai_error = message
            if block_on_error:
                self.unit.status = BlockedStatus(message)

        mode = self._mode()
        if mode == Mode.ACT:
            logger.warning("act mode is not yet implemented — ignoring")
        if mode != Mode.SUGGEST:
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

        # Bound the prompt built from the stored report; the report file on
        # disk is untouched. 0 (or an unset option, in a test harness that
        # does not declare it) disables projection: the full report is sent.
        budget = self.model.config.get("max-prompt-bytes", 0)
        prompt_report = build_projection(report_content, budget)
        self._last_projection = (
            {
                "version": PROJECTION_VERSION,
                "budget_bytes": budget,
                "report_bytes": len(report_content.encode()),
                "prompt_bytes": len(prompt_report.encode()),
            }
            if budget > 0
            else None
        )

        try:
            if question:
                return run_ask(provider, prompt_report, question, history)
            return run_suggest(provider, prompt_report, additional_context)
        except Exception as e:
            logger.error("AI provider call failed: %s", traceback.format_exc())
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
        now = utc_now()
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

        The turn records the incident's current report version, the one its
        prompt was built from. Returns the persisted turn, carrying its
        assigned per-incident id.
        """
        incident = Incident.from_dict(incident_dict)
        turn = dataclasses.replace(turn, report_file=turn.report_file or incident.report_name)
        updated = incident.append_turn(turn)
        turn = updated.turns[-1]
        updated = updated.prune_turns()
        self._status_tracker.update_incident(unit_name, updated.to_dict())
        self._status_tracker.set_ai_error(unit_name, "")

        now = utc_now_iso()
        extra = {"usage": turn.usage.to_dict()} if turn.usage is not None else {}
        if getattr(self, "_last_projection", None) is not None:
            extra["projection"] = self._last_projection
        self._audit(
            "suggestion-generated",
            now,
            unit=unit_name,
            incident_id=incident.id,
            turn_id=turn.id,
            question=turn.question,
            history_turns=history_count,
            commands=list(turn.commands),
            report_file=turn.report_file,
            mode=self.model.config.get("mode", Mode.OBSERVE.value),
            **extra,
        )

        if turn.usage is not None:
            usage_entry = turn.usage.to_dict()
            usage_entry["incident_id"] = incident.id
            usage_entry["turn_id"] = turn.id
            usage_entry["timestamp"] = now
            self._status_tracker.record_usage(unit_name, usage_entry)

        return turn

    # ------------------------------------------------------------------
    # Incident lifecycle (shared state machine)
    # ------------------------------------------------------------------

    @staticmethod
    def _status_rank(status: StatusBase) -> int:
        """Severity used to pick one unit status from several per-unit results."""
        if isinstance(status, BlockedStatus):
            return 3
        if isinstance(status, ActiveStatus) and status.message.startswith("incident open"):
            return 2
        if isinstance(status, (WaitingStatus, MaintenanceStatus)):
            return 1
        return 0

    def _process_units(self, observations: list[tuple[str, str, str, str]]) -> None:
        """Drive the incident lifecycle for every observed unit, then set one status.

        ``observations`` holds ``(unit, status, since_iso, message)`` tuples.
        The unit status is the most severe per-unit result, so a healthy unit
        processed last cannot hide another unit's incident.
        """
        results = [self._process_unit(*observation) for observation in observations]
        if results:
            self.unit.status = max(results, key=self._status_rank)

    def _process_unit(self, unit_name: str, status: str, since_iso: str, message: str = "") -> StatusBase:
        """Drive the incident lifecycle for a single observed unit.

        ``message`` is the workload status message shown by ``juju status``
        (e.g. "Please initialize OpenBao or integrate with an auto-unseal
        provider"). Empty when the observing path cannot see it — the machine
        charm's goal-state hook tool carries no message.

        Sets and returns the unit's resulting status; ``_process_units``
        reconciles the results of several units.
        """
        now = utc_now()
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
            return self._set_status(ActiveStatus(self._ready_message()))

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
        first_seen_dt = parse_ts(first_seen)
        # An unknown first-seen time is treated as stale.
        unhealthy_minutes = (now - first_seen_dt).total_seconds() / 60 if first_seen_dt else failure_timeout

        if unhealthy_minutes < failure_timeout:
            logger.debug(
                "unit %s: unhealthy for %.1f min, waiting for failure-timeout (%d min)",
                unit_name,
                unhealthy_minutes,
                failure_timeout,
            )
            return self._set_status(
                WaitingStatus(f"{status} - waiting ({unhealthy_minutes:.1f}/{failure_timeout} min)")
            )

        # --- Incident already open: cooldown, then refresh the same incident ---
        incident_dict = self._status_tracker.current_incident(unit_name) if had_open_incident else None
        if incident_dict:
            last_reported_dt = parse_ts(self._status_tracker.last_reported(unit_name))
            cooldown_elapsed = (now - last_reported_dt).total_seconds() / 60 if last_reported_dt else cooldown
            if cooldown_elapsed < cooldown:
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
            else:
                self._refresh_on_cooldown(unit_name, status, message, first_seen, incident_dict, ts)
            return self._set_status(self._incident_status(unit_name, status, incident_dict))

        return self._set_status(self._open_incident(unit_name, status, message, first_seen, since_iso, increment, ts))

    def _set_status(self, status: StatusBase) -> StatusBase:
        self.unit.status = status
        return status

    def _incident_status(self, unit_name: str, workload: str, incident_dict: dict) -> StatusBase:
        """Status for a unit with an open incident: blocked on a stored AI error."""
        ai_error = self._status_tracker.ai_error(unit_name)
        if ai_error:
            return BlockedStatus(ai_error)
        return ActiveStatus(f"incident open: {unit_name} {workload} ({incident_dict.get('id', '')[:8]})")

    def _write_incident_report(
        self,
        unit_name: str,
        workload: str,
        first_seen: str,
        message: str,
        incident_id: str,
        versioned: bool,
    ) -> tuple[dict, str]:
        """Collect context and write a report for an incident: ``(context, path)``."""
        context = self._collect_context(unit_name, parse_ts(first_seen))
        path = generate_report(
            incident_id=incident_id,
            unit_name=unit_name,
            workload=workload,
            first_seen=first_seen,
            context=context,
            report_dir=self.model.config.get("report-dir", ""),
            status_message=message,
            versioned=versioned,
        )
        return context, path

    def _audit_report(self, ts: str, unit_name: str, incident_id: str, context: dict, path: str, reason: str) -> None:
        self._audit(
            "context-collected",
            ts,
            unit=unit_name,
            incident_id=incident_id,
            log_lines=len(context.get("unit_logs", [])),
        )
        self._audit("report-generated", ts, unit=unit_name, incident_id=incident_id, report_path=path, reason=reason)
        logger.info("incident %s: report written to %s", incident_id[:8], path)

    def _refresh_report(self, unit_name: str, entry: dict, incident_dict: dict, ts: str, reason: str) -> str:
        """Write a new report version for an open incident and make it the latest.

        ``entry`` is the unit's tracker entry. Raises when collection or report
        writing fails, leaving the incident and its earlier versions untouched.
        """
        first_seen = entry.get("unhealthy_since") or entry.get("since", "")
        context, path = self._write_incident_report(
            unit_name,
            entry.get("status", "unknown"),
            first_seen,
            entry.get("message", ""),
            incident_dict["id"],
            versioned=True,
        )
        updated = Incident.from_dict(incident_dict).with_report(os.path.basename(path))
        self._status_tracker.record_reported(unit_name, ts, updated.to_dict())
        self._audit_report(ts, unit_name, updated.id, context, path, reason)
        return path

    def _refresh_on_cooldown(
        self, unit_name: str, workload: str, message: str, first_seen: str, incident_dict: dict, ts: str
    ) -> None:
        """Refresh the report of a still-unhealthy unit once its cooldown elapsed.

        No AI call is made; ``get-suggestion`` and ``ask`` use the new version.
        A failure is audited and retried after the next cooldown period.
        """
        self.unit.status = MaintenanceStatus(f"refreshing report: {unit_name} ({incident_dict['id'][:8]})")
        entry = {"status": workload, "message": message, "unhealthy_since": first_seen}
        try:
            self._refresh_report(unit_name, entry, incident_dict, ts, reason="refresh")
        except Exception as e:
            logger.error("incident %s: report refresh failed: %s", incident_dict["id"][:8], traceback.format_exc())
            self._audit("incident-report-failed", ts, unit=unit_name, workload=workload, reason="refresh", error=str(e))
            self._status_tracker.record_reported(unit_name, ts, incident_dict)

    def _open_incident(
        self,
        unit_name: str,
        status: str,
        message: str,
        first_seen: str,
        since_iso: str,
        increment: int,
        ts: str,
    ) -> StatusBase:
        """Open an incident: report first, then record it, then suggest.

        The incident is recorded only once its report is on disk. If
        collection or report writing fails, nothing is recorded, an
        ``incident-report-failed`` event is audited, and the next cycle retries.
        """
        incident = Incident.open(status_message=message)
        short_id = incident.id[:8]

        self.unit.status = MaintenanceStatus(f"collecting context: {unit_name} ({short_id})")
        try:
            context, report_path = self._write_incident_report(
                unit_name, status, first_seen, message, incident.id, versioned=False
            )
        except Exception as e:
            logger.error("incident for %s not opened, report failed: %s", unit_name, traceback.format_exc())
            self._audit("incident-report-failed", ts, unit=unit_name, workload=status, reason="open", error=str(e))
            return BlockedStatus(f"report failed for {unit_name}: {str(e)[:60]}")

        incident = incident.with_report(os.path.basename(report_path))
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
        self._audit_report(ts, unit_name, incident.id, context, report_path, reason="open")

        # Suggest: the provider reads the stored report, never the live context.
        with open(report_path) as f:
            report_content = f.read()

        suggestion = self._run_mode_logic(report_content, block_on_error=False)
        if suggestion is not None:
            self._store_suggestion(unit_name, incident.to_dict(), suggestion)
        elif self._last_ai_error:
            self._status_tracker.set_ai_error(unit_name, self._last_ai_error)
        return self._incident_status(unit_name, status, incident.to_dict())
