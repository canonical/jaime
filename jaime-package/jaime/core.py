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

import datetime
import hashlib
import json
import logging
import os
import traceback
from enum import Enum

from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

from jaime.incident import Incident, Suggestion
from jaime.logging import write_event
from jaime.report import generate_report
from jaime.suggest import run_act, run_suggest

logger = logging.getLogger(__name__)


class Mode(str, Enum):
    OBSERVE = "observe"
    SUGGEST = "suggest"
    ACT = "act"


class Provider(str, Enum):
    NONE = "none"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


def summarise_usage(entries: list[dict]) -> dict:
    """Aggregate a list of usage log entries into a summary with per-model breakdown."""
    prompt_tokens = sum(e.get("prompt_tokens", 0) for e in entries)
    completion_tokens = sum(e.get("completion_tokens", 0) for e in entries)
    total_tokens = sum(e.get("total_tokens", 0) for e in entries)
    costs = [e["cost_usd"] for e in entries if e.get("cost_usd") is not None]

    by_model = {}
    for e in entries:
        model = e.get("model") or "unknown"
        if model not in by_model:
            by_model[model] = {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": None,
            }
        m = by_model[model]
        m["calls"] += 1
        m["prompt_tokens"] += e.get("prompt_tokens", 0)
        m["completion_tokens"] += e.get("completion_tokens", 0)
        m["total_tokens"] += e.get("total_tokens", 0)
        if e.get("cost_usd") is not None:
            m["cost_usd"] = (m["cost_usd"] or 0.0) + e["cost_usd"]

    for m in by_model.values():
        if m["cost_usd"] is not None:
            m["cost_usd"] = round(m["cost_usd"], 6)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "by_model": by_model,
    }


class CoreMixin:
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
            self.unit.status = BlockedStatus(
                f"invalid mode '{self.model.config.get('mode')}', must be one of: {valid}"
            )
            return

        try:
            Provider(self.model.config.get("provider", Provider.NONE))
        except ValueError:
            valid = ", ".join(p.value for p in Provider)
            self.unit.status = BlockedStatus(
                f"invalid provider '{self.model.config.get('provider')}', must be one of: {valid}"
            )
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
        for unit_name, entry in self._status_tracker._state.items():
            inc = entry.get("incident")
            if inc and inc.get("closed_at") is None:
                short_id = inc.get("id", "")[:8]
                self.unit.status = ActiveStatus(
                    f"incident open: {unit_name} "
                    f"{entry.get('status', 'unknown')} ({short_id})"
                )
                return
        self.unit.status = ActiveStatus("Ready")

    def _watch_statuses(self) -> set[str]:
        return {
            s.strip()
            for s in self.model.config.get("watch-statuses", "error,blocked").split(",")
            if s.strip()
        }

    # ------------------------------------------------------------------
    # AI provider
    # ------------------------------------------------------------------

    def _get_ai_provider(self):
        try:
            provider_name = Provider(self.model.config.get("provider", Provider.NONE))
        except ValueError:
            return None, f"unsupported provider '{self.model.config.get('provider')}'"

        if provider_name == Provider.NONE:
            return None, (
                f"mode={self.model.config.get('mode')} but provider is not configured"
            )

        api_token = self._resolve_api_token()
        if not api_token:
            logger.warning(
                "provider '%s' configured but api-token is empty", provider_name.value
            )
            return None, f"provider={provider_name.value} but api-token is not set"

        model = self.model.config.get("model", "") or self._default_model(
            provider_name.value
        )

        if provider_name == Provider.GEMINI:
            from jaime.providers.gemini import GeminiProvider
            return GeminiProvider(api_token, model), None
        elif provider_name == Provider.OPENROUTER:
            from jaime.providers.openrouter import OpenRouterProvider
            return OpenRouterProvider(api_token, model), None

        return None, f"unsupported provider '{provider_name.value}'"

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

    def _run_mode_logic(self, report_content: str,
                        additional_context: str = "") -> Suggestion | None:
        """Run suggest or act logic based on the configured mode."""
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
            self.unit.status = BlockedStatus(provider_err)
            return None

        err = provider.check()
        if err:
            logger.error("AI provider token check failed before generation: %s", err)
            self.unit.status = BlockedStatus(f"AI provider error: {err[:100]}")
            return None

        try:
            if mode == Mode.SUGGEST:
                return run_suggest(provider, report_content, additional_context)

            suggestion, act_results = run_act(provider, report_content, additional_context)
            for result in act_results:
                write_event({
                    "event": "act-command-executed",
                    "command": result["command"],
                    "returncode": result["returncode"],
                    "stderr": result.get("stderr", ""),
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }, self.model.config.get("audit-log-path", ""))
            return suggestion

        except Exception as e:
            logger.error("AI provider call failed in mode '%s': %s", mode.value,
                         traceback.format_exc())
            self.unit.status = BlockedStatus(f"AI provider error: {str(e)[:100]}")
            return None

    def _store_suggestion(self, unit_name: str, incident_dict: dict,
                          suggestion: Suggestion) -> Suggestion:
        """Attach a suggestion to the incident, persist it, and record token usage."""
        updated = Incident.from_dict(incident_dict).attach_suggestion(suggestion)
        self._status_tracker.update_incident(unit_name, updated.to_dict())

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit_path = self.model.config.get("audit-log-path", "")

        audit_event = {
            "event": "suggestion-generated",
            "unit": unit_name,
            "incident_id": incident_dict["id"],
            "commands": list(suggestion.commands),
            "mode": self.model.config.get("mode", Mode.OBSERVE.value),
            "timestamp": now,
        }
        if suggestion.usage is not None:
            audit_event["usage"] = suggestion.usage.to_dict()
        write_event(audit_event, audit_path)

        if suggestion.usage is not None:
            usage_entry = suggestion.usage.to_dict()
            usage_entry["incident_id"] = incident_dict["id"]
            usage_entry["timestamp"] = now
            self._status_tracker.record_usage(unit_name, usage_entry)

        return suggestion

    # ------------------------------------------------------------------
    # Incident lifecycle (shared state machine)
    # ------------------------------------------------------------------

    def _process_unit(self, unit_name: str, status: str, since_iso: str) -> None:
        """Drive the incident lifecycle for a single observed unit."""
        now = datetime.datetime.now(datetime.timezone.utc)
        watch_statuses = self._watch_statuses()
        failure_timeout = self.model.config.get("failure-timeout-minutes", 5)
        cooldown = self.model.config.get("cooldown-minutes", 30)

        watched = status in watch_statuses
        had_open_incident = self._status_tracker.has_open_incident(unit_name)
        prior_incident = self._status_tracker.current_incident(unit_name)
        increment = self._status_tracker.observe(
            unit_name, status, since_iso, watched
        )

        # --- Recovery ---
        if not watched:
            if increment == 1 and had_open_incident and prior_incident:
                closed = Incident.from_dict(prior_incident).close()
                self._status_tracker.close_incident(unit_name, closed.to_dict())
                logger.info(json.dumps({
                    "event": "incident-closed",
                    "unit": unit_name,
                    "workload": status,
                    "incident": closed.to_dict(),
                    "timestamp": now.isoformat(),
                }))
            elif increment == 1:
                logger.debug(json.dumps({
                    "event": "principal-status-recovered",
                    "unit": unit_name,
                    "workload": status,
                    "timestamp": now.isoformat(),
                }))
            else:
                logger.debug(
                    "unit %s: workload=%s (not watched, increment=%d)",
                    unit_name, status, increment,
                )
            self.unit.status = ActiveStatus("Ready")
            return

        # Time the incident from when Jaime first saw this unit go unhealthy,
        # not from Juju's `since`. Juju bumps `since` every time a charm
        # re-sets its status — including re-setting the same status with a new
        # message, or flapping between two watched statuses — so a workload
        # stuck in a retry loop would otherwise reset the timer forever and
        # never reach failure-timeout-minutes.
        first_seen = self._status_tracker.unhealthy_since(unit_name) or since_iso

        logger.debug(json.dumps({
            "event": "principal-status-watched",
            "unit": unit_name,
            "workload": status,
            "first_seen": first_seen,
            "status_since": since_iso,
            "increment": increment,
            "timestamp": now.isoformat(),
        }))

        # --- Within failure-timeout: waiting ---
        try:
            since_dt = datetime.datetime.fromisoformat(first_seen)
            unhealthy_minutes = (now - since_dt).total_seconds() / 60
        except ValueError:
            unhealthy_minutes = failure_timeout  # unknown since: treat as stale

        if unhealthy_minutes < failure_timeout:
            logger.debug(
                "unit %s: unhealthy for %.1f min, waiting for failure-timeout (%d min)",
                unit_name, unhealthy_minutes, failure_timeout,
            )
            self.unit.status = WaitingStatus(
                f"{status} - waiting ({unhealthy_minutes:.1f}/{failure_timeout} min)"
            )
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
                logger.debug(json.dumps({
                    "event": "principal-status-cooldown",
                    "unit": unit_name,
                    "workload": status,
                    "first_seen": first_seen,
                    "increment": increment,
                    "cooldown_elapsed_minutes": round(cooldown_elapsed, 1),
                    "cooldown_minutes": cooldown,
                    "incident": incident_dict,
                    "timestamp": now.isoformat(),
                }))
                # Preserve BlockedStatus if a provider error was already set.
                if not isinstance(self.unit.status, BlockedStatus):
                    self.unit.status = ActiveStatus(
                        f"incident open: {unit_name} {status} ({short_id})"
                    )
                return

        # --- Open a new incident ---
        incident = Incident.open()
        short_id = incident.id[:8]
        logger.info(json.dumps({
            "event": "incident-opened",
            "unit": unit_name,
            "workload": status,
            "first_seen": first_seen,
            "status_since": since_iso,
            "increment": increment,
            "incident": incident.to_dict(),
            "timestamp": now.isoformat(),
        }))
        self._status_tracker.record_reported(
            unit_name, now.isoformat(), incident.to_dict()
        )
        write_event({
            "event": "incident-start",
            "unit": unit_name,
            "workload": status,
            "first_seen": first_seen,
            "status_since": since_iso,
            "incident_id": incident.id,
            "timestamp": now.isoformat(),
        }, self.model.config.get("audit-log-path", ""))

        # Collect context and generate report via the substrate hook.
        self.unit.status = MaintenanceStatus(
            f"collecting context: {unit_name} ({short_id})"
        )
        context = self._collect_incident_context(unit_name, first_seen, incident)

        report_path = generate_report(
            incident_id=incident.id,
            unit_name=unit_name,
            workload=status,
            first_seen=first_seen,
            context=context,
            report_dir=self.model.config.get("report-dir", ""),
        )
        write_event({
            "event": "report-generated",
            "unit": unit_name,
            "incident_id": incident.id,
            "report_path": report_path,
            "timestamp": now.isoformat(),
        }, self.model.config.get("audit-log-path", ""))
        logger.info("incident %s: report written to %s", short_id, report_path)

        # Run suggest/act: produce a Suggestion and attach it to the incident.
        with open(report_path) as f:
            report_content = f.read()

        suggestion = self._run_mode_logic(report_content)
        if suggestion is not None:
            self._store_suggestion(unit_name, incident.to_dict(), suggestion)
        if not isinstance(self.unit.status, BlockedStatus):
            self.unit.status = ActiveStatus(
                f"incident open: {unit_name} {status} ({short_id})"
            )

    # ------------------------------------------------------------------
    # Shared action handlers
    # ------------------------------------------------------------------

    def _on_action_show_status(self, event):
        state = self._status_tracker._state
        if not state:
            event.set_results({"result": "no status observed yet"})
            return
        results = {}
        for unit_name, entry in state.items():
            results.update({
                "unit": unit_name,
                "workload": entry.get("status", "unknown"),
                "first-seen": entry.get("unhealthy_since") or entry.get("since", ""),
                "status-since": entry.get("since", ""),
                "increment": str(entry.get("increment", 0)),
                "last-reported": entry.get("last_reported") or "",
                "incident-id": (entry.get("incident") or {}).get("id", ""),
                "incident-opened-at": (entry.get("incident") or {}).get("opened_at", ""),
            })
        event.set_results(results)

    def _on_action_show_usage(self, event):
        incident_id = event.params.get("incident-id", "").strip()
        all_entries = self._status_tracker.all_usage_log()

        if not all_entries:
            event.set_results({"result": json.dumps(
                {"message": "no usage recorded yet"}, indent=2)})
            return

        if incident_id:
            entries = [e for e in all_entries if e.get("incident_id") == incident_id]
            if not entries:
                event.fail(f"no usage found for incident {incident_id}")
                return
            summary = summarise_usage(entries)
            summary["incident_id"] = incident_id
        else:
            summary = summarise_usage(all_entries)
            summary["total_incidents"] = len({e.get("incident_id") for e in all_entries})
            summary["total_calls"] = len(all_entries)

        event.set_results({"result": json.dumps(summary, indent=2)})

    def _on_action_get_suggestion(self, event):
        additional_context = event.params.get("additional-context", "")
        context_hash = (
            hashlib.sha256(additional_context.encode()).hexdigest()
            if additional_context else ""
        )

        current_provider = self.model.config.get("provider", "none")
        current_model = (
            self.model.config.get("model", "") or self._default_model(current_provider)
        )

        for unit_name, entry in self._status_tracker._state.items():
            inc = entry.get("incident")
            if inc and inc.get("closed_at") is None:
                suggestion = inc.get("suggestion")
                if suggestion:
                    stored_hash = suggestion.get("context_hash", "")
                    stored_model = (suggestion.get("usage") or {}).get("model", "")
                    model_changed = stored_model and stored_model != current_model
                    if not model_changed and (not additional_context or context_hash == stored_hash):
                        event.set_results({
                            "incident-id": inc["id"],
                            "description": suggestion["description"],
                            "commands": "\n".join(suggestion["commands"]),
                            "command-count": len(suggestion["commands"]),
                            "generated-at": suggestion["generated_at"],
                            "cached": "true",
                        })
                        return
                    if model_changed:
                        logger.info(
                            "model changed from '%s' to '%s' — regenerating suggestion",
                            stored_model, current_model,
                        )
                    else:
                        logger.info("additional-context hash differs — regenerating suggestion")

                mode = Mode(self.model.config.get("mode", Mode.OBSERVE))
                if mode == Mode.OBSERVE:
                    event.fail("no suggestion available: mode is 'observe'")
                    return

                report_dir = self.model.config.get("report-dir", "") or "/var/log/jaime/reports"
                report_path = os.path.join(report_dir, f'{inc["id"]}.md')
                if not os.path.exists(report_path):
                    event.fail(f"report file not found at {report_path}")
                    return
                with open(report_path) as f:
                    report_content = f.read()

                suggestion = self._run_mode_logic(report_content, additional_context)
                if suggestion is None:
                    event.fail("could not generate suggestion")
                    return

                self._store_suggestion(unit_name, inc, suggestion)
                results = {
                    "incident-id": inc["id"],
                    "description": suggestion.description,
                    "commands": "\n".join(suggestion.commands),
                    "command-count": len(suggestion.commands),
                    "generated-at": suggestion.generated_at,
                    "cached": "false",
                }
                if suggestion.usage is not None:
                    results["model"] = suggestion.usage.model
                    results["prompt-tokens"] = str(suggestion.usage.prompt_tokens)
                    results["completion-tokens"] = str(suggestion.usage.completion_tokens)
                    results["total-tokens"] = str(suggestion.usage.total_tokens)
                    if suggestion.usage.cost_usd is not None:
                        results["cost-usd"] = f"{suggestion.usage.cost_usd:.6f}"
                event.set_results(results)
                return

        event.fail("no open incident found")

    def _on_action_generate_report(self, event):
        for unit_name, entry in self._status_tracker._state.items():
            inc = entry.get("incident")
            if inc and inc.get("closed_at") is None:
                since_iso = entry.get("unhealthy_since") or entry.get("since", "")
                context = self._collect_report_context(unit_name, since_iso)
                report_path = generate_report(
                    incident_id=inc["id"],
                    unit_name=unit_name,
                    workload=entry.get("status", "unknown"),
                    first_seen=since_iso,
                    context=context,
                    report_dir=self.model.config.get("report-dir", ""),
                )
                event.set_results({
                    "incident-id": inc["id"],
                    "report-path": report_path,
                })
                return
        event.fail("no open incident found")

    def _on_action_reset(self, event):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for unit_name in list(self._status_tracker._state):
            if self._status_tracker.has_open_incident(unit_name):
                incident_dict = self._status_tracker.current_incident(unit_name)
                closed = Incident.from_dict(incident_dict).close()
                logger.info(json.dumps({
                    "event": "incident-closed",
                    "unit": unit_name,
                    "reason": "manual reset",
                    "incident": closed.to_dict(),
                    "timestamp": now,
                }))
        self._status_tracker.reset()
        self.unit.status = ActiveStatus("Ready")
        logger.info("status state cleared")
        event.set_results({"result": "status state cleared"})
