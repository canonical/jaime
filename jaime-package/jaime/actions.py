"""Juju action handlers shared by both Jaime charms.

``ActionsMixin`` holds every ``_on_action_*`` handler. It reads charm state
(``self.model``, ``self._status_tracker``) and calls back into ``CoreMixin``
for the AI pipeline (``_run_mode_logic``, ``_store_turn``,
``_store_suggestion``) and the substrate hooks, so it is only meant to be
mixed into ``CoreMixin``.
"""

import hashlib
import json
import logging
import os

from ops.model import ActiveStatus

from jaime.incident import Incident, Turn
from jaime.logging import list_incidents, rotate_audit_log
from jaime.modes import Mode
from jaime.redact import redact_text
from jaime.report import report_path
from jaime.timeutil import utc_now_iso

logger = logging.getLogger(__name__)


_USAGE_COUNTERS = ("prompt_tokens", "completion_tokens", "total_tokens")


def summarise_usage(entries: list[dict], folded: dict[str, dict] | None = None) -> dict:
    """Aggregate usage log entries into a summary with per-model breakdown.

    ``folded`` holds per-model totals of entries already folded out of the
    log (``StatusTracker.folded_usage``); they count towards every total.
    """
    by_model: dict[str, dict] = {}

    def add(model: str, calls: int, counts: dict, cost) -> None:
        m = by_model.setdefault(model, {"calls": 0, **{k: 0 for k in _USAGE_COUNTERS}, "cost_usd": None})
        m["calls"] += calls
        for key in _USAGE_COUNTERS:
            m[key] += counts.get(key, 0)
        if cost is not None:
            m["cost_usd"] = (m["cost_usd"] or 0.0) + cost

    for model, totals in (folded or {}).items():
        add(model, totals.get("calls", 0), totals, totals.get("cost_usd"))
    for e in entries:
        add(e.get("model") or "unknown", 1, e, e.get("cost_usd"))

    costs = [m["cost_usd"] for m in by_model.values() if m["cost_usd"] is not None]
    for m in by_model.values():
        if m["cost_usd"] is not None:
            m["cost_usd"] = round(m["cost_usd"], 6)

    return {
        **{key: sum(m[key] for m in by_model.values()) for key in _USAGE_COUNTERS},
        "cost_usd": round(sum(costs), 6) if costs else None,
        "by_model": by_model,
    }


class ActionsMixin:
    """The Juju action handlers of the Jaime charms."""

    def _on_action_show_status(self, event):
        """Return the current monitoring state for every monitored unit.

        Juju action results are a flat map of scalars, so the per-unit records
        are returned as a JSON array under ``result`` rather than as separate
        keys. A single dict keyed by fixed field names would let each unit
        overwrite the previous one, which is how this action previously
        reported only an arbitrary single unit.

        Records are filtered to the applications resolved as monitored, so an
        application removed from configuration disappears from the action. The
        tracker keeps the observation itself for the incident history.
        """
        monitored = set(self._monitored_applications())
        records = [
            {
                "unit": unit_name,
                "workload": entry.get("status", "unknown"),
                "status-message": entry.get("message", ""),
                "first-seen": entry.get("unhealthy_since") or entry.get("since", ""),
                "status-since": entry.get("since", ""),
                "increment": entry.get("increment", 0),
                "last-reported": entry.get("last_reported") or "",
                "incident-id": (entry.get("incident") or {}).get("id", ""),
                "incident-opened-at": (entry.get("incident") or {}).get("opened_at", ""),
            }
            for unit_name, entry in self._status_tracker.entries()
            if unit_name.split("/")[0] in monitored
        ]
        event.set_results({"result": json.dumps(records, indent=2)})

    def _on_action_show_usage(self, event):
        """Usage summary. Folded (old) entries count in the global totals only.

        Folded entries no longer carry an incident id, so a per-incident view
        and ``total_incidents`` only see the retained entries.
        """
        incident_id = event.params.get("incident-id", "").strip()
        all_entries = self._status_tracker.all_usage_log()
        folded = self._status_tracker.folded_usage()

        if not all_entries and not folded:
            event.set_results({"result": json.dumps({"message": "no usage recorded yet"}, indent=2)})
            return

        if incident_id:
            entries = [e for e in all_entries if e.get("incident_id") == incident_id]
            if not entries:
                event.fail(f"no usage found for incident {incident_id}")
                return
            summary = summarise_usage(entries)
            summary["incident_id"] = incident_id
        else:
            summary = summarise_usage(all_entries, folded)
            summary["total_incidents"] = len({e.get("incident_id") for e in all_entries})
            summary["total_calls"] = len(all_entries) + sum(m.get("calls", 0) for m in folded.values())

        event.set_results({"result": json.dumps(summary, indent=2)})

    def _on_action_get_suggestion(self, event):
        additional_context = event.params.get("additional-context", "")
        force = bool(event.params.get("force", False))
        context_hash = hashlib.sha256(additional_context.encode()).hexdigest() if additional_context else ""

        open_incidents = self._status_tracker.incidents()
        if not open_incidents:
            event.fail("no open incident found")
            return
        unit_name, _entry, inc = open_incidents[0]

        cached = None if force else self._cached_suggestion(inc, additional_context, context_hash)
        if cached:
            commands = cached.get("commands") or []
            event.set_results(
                {
                    "incident-id": inc["id"],
                    "turn-id": cached.get("id", ""),
                    "description": cached["description"],
                    "commands": "\n".join(commands),
                    "command-count": len(commands),
                    "generated-at": cached["generated_at"],
                    "cached": "true",
                }
            )
            return

        mode = self._mode()
        if mode is None:
            event.fail(f"invalid mode '{self.model.config.get('mode')}'")
            return
        if mode == Mode.OBSERVE:
            event.fail("no suggestion available: mode is 'observe'")
            return

        report_content, error = self._read_report(inc)
        if error:
            event.fail(error)
            return

        suggestion = self._run_mode_logic(report_content, additional_context)
        if suggestion is None:
            if self._last_ai_error:
                self._status_tracker.set_ai_error(unit_name, self._last_ai_error)
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
        results.update(self._usage_results(suggestion.usage))
        event.set_results(results)

    def _cached_suggestion(self, inc: dict, additional_context: str, context_hash: str) -> dict | None:
        """The stored one-shot suggestion if it still answers this request.

        Ask turns answer a specific question, so only the latest turn with an
        empty question (or the legacy single suggestion) can serve as the
        cache. It is stale when the configured model changed, when a
        different ``additional-context`` was supplied, or when it was based on
        an older report version than the incident's latest.
        """
        turns = inc.get("turns") or []
        oneshot = next((t for t in reversed(turns) if not t.get("question")), None)
        suggestion = oneshot or (None if turns else inc.get("suggestion"))
        if not suggestion:
            return None

        original = f"{inc['id']}.md"
        if (suggestion.get("report_file") or original) != (inc.get("report_file") or original):
            logger.info("report refreshed since the stored suggestion - regenerating suggestion")
            return None

        current_provider = self.model.config.get("provider", "none")
        current_model = self.model.config.get("model", "") or self._default_model(current_provider)
        stored_model = (suggestion.get("usage") or {}).get("model", "")
        if stored_model and stored_model != current_model:
            logger.info(
                "model changed from '%s' to '%s' - regenerating suggestion",
                stored_model,
                current_model,
            )
            return None
        if additional_context and context_hash != suggestion.get("context_hash", ""):
            logger.info("additional-context hash differs - regenerating suggestion")
            return None
        return suggestion

    def _read_report(self, inc: dict) -> tuple[str, str]:
        """Read an incident's latest report version: ``(content, error)``."""
        path = report_path(self.model.config.get("report-dir", ""), Incident.from_dict(inc).report_name)
        if not os.path.exists(path):
            return "", f"report file not found at {path}"
        with open(path) as f:
            return f.read(), ""

    @staticmethod
    def _usage_results(usage) -> dict:
        """Action result keys describing an LLM call's token usage and cost."""
        if usage is None:
            return {}
        results = {
            "model": usage.model,
            "prompt-tokens": str(usage.prompt_tokens),
            "completion-tokens": str(usage.completion_tokens),
            "total-tokens": str(usage.total_tokens),
        }
        if usage.cost_usd is not None:
            results["cost-usd"] = f"{usage.cost_usd:.6f}"
        return results

    def _on_action_ask(self, event):
        """Append one conversational turn to an incident (never overwrites)."""
        question = (event.params.get("question") or "").strip()
        if not question:
            event.fail("question is required")
            return
        # The question goes to the provider and is stored, so scrub secrets an
        # operator may have pasted into it, as the report is scrubbed.
        question = redact_text(question)
        incident_id = (event.params.get("incident-id") or "").strip()

        mode = self._mode()
        if mode != Mode.SUGGEST:
            current = mode.value if mode else self.model.config.get("mode")
            event.fail(f"ask requires mode=suggest (current mode is '{current}')")
            return

        # An explicit id may name a closed incident (follow-up after recovery);
        # without one, the question goes to the single open incident.
        matches = [(unit_name, inc) for unit_name, _entry, inc in self._status_tracker.incidents(incident_id)]
        if not matches:
            event.fail(
                "no matching incident found"
                if incident_id
                else "no open incident found; pass incident-id to ask about a closed one"
            )
            return
        if len(matches) > 1:
            ids = ", ".join(inc["id"] for _, inc in matches)
            event.fail(f"several open incidents, pass incident-id (one of: {ids})")
            return
        unit_name, inc = matches[0]

        report_content, error = self._read_report(inc)
        if error:
            event.fail(error)
            return

        history = [
            {
                "id": t.get("id", ""),
                "question": t.get("question", ""),
                "description": t.get("description", ""),
                "commands": t.get("commands") or [],
            }
            for t in inc.get("turns") or []
        ]

        suggestion = self._run_mode_logic(
            report_content,
            question=question,
            history=history,
            block_on_error=False,
        )
        if suggestion is None:
            reason = self._last_ai_error or "no answer was generated"
            event.fail(f"could not answer the question: {reason}")
            return

        turn = Turn.from_suggestion(suggestion, question=question)
        turn = self._store_turn(unit_name, inc, turn, history_count=len(history))
        results = {
            "incident-id": inc["id"],
            "turn-id": turn.id,
            "description": turn.description,
            "commands": "\n".join(turn.commands),
            "command-count": len(turn.commands),
            "generated-at": turn.generated_at,
        }
        results.update(self._usage_results(turn.usage))
        event.set_results(results)

    def _on_action_list_suggestions(self, event):
        """Return the conversation for an incident as a JSON array."""
        incident_id = (event.params.get("incident-id") or "").strip()
        records = []
        for unit_name, _entry, inc in self._status_tracker.incidents(incident_id, include_closed=True):
            for turn in inc.get("turns") or []:
                records.append(
                    {
                        "incident_id": inc["id"],
                        "unit": unit_name,
                        "turn_id": turn.get("id", ""),
                        "question": turn.get("question", ""),
                        "generated_at": turn.get("generated_at", ""),
                        "report_file": turn.get("report_file") or f"{inc['id']}.md",
                        "description": turn.get("description", ""),
                    }
                )
        event.set_results({"result": json.dumps(records)})

    def _on_action_generate_report(self, event):
        """Write a new report version for the open incident; never overwrite one.

        The new version becomes the incident's latest, the one ``ask`` and
        ``get-suggestion`` use; earlier versions are kept unchanged.
        """
        for unit_name, entry, inc in self._status_tracker.incidents():
            try:
                with self._hook_cache():
                    path = self._refresh_report(unit_name, entry, inc, utc_now_iso(), reason="manual")
            except Exception as e:
                logger.error("generate-report failed for %s: %s", unit_name, e)
                event.fail(f"could not generate report: {e}")
                return
            event.set_results({"incident-id": inc["id"], "report-path": path})
            return
        event.fail("no open incident found")

    def _on_action_reset(self, event):
        now = utc_now_iso()
        audit_path = self.model.config.get("audit-log-path", "")

        # Close every incident still open in the audit log as well as the ones
        # the tracker carries. events.jsonl is append-only and remembers every
        # incident ever opened, while status-state.json keeps only the latest
        # per unit - so a reset that closes only the tracked incidents would
        # leave older audit rows reported as open by list-incidents forever.
        # Append a closure row per open incident; we never rewrite history, and
        # an incident already closed gets no second row.
        open_records = [r for r in list_incidents(audit_path) if r["status"] == "open"]
        closed_incident_ids = set()
        for record in open_records:
            closed_incident_ids.add(record["incident_id"])
            self._audit(
                "incident-closed",
                now,
                unit=record["unit"],
                reason="manual reset",
                incident_id=record["incident_id"],
                closed_at=now,
            )

        for unit_name in self._status_tracker.units():
            if self._status_tracker.has_open_incident(unit_name):
                incident_dict = self._status_tracker.current_incident(unit_name)
                closed = Incident.from_dict(incident_dict).close()
                self._log_event(
                    logging.INFO,
                    "incident-closed",
                    now,
                    unit=unit_name,
                    reason="manual reset",
                    incident=closed.to_dict(),
                )
                # The list_incidents pass above already closed this incident in
                # the audit log; write only if it was not found there, so an
                # incident never gets two closure rows for the same reset.
                if closed.id not in closed_incident_ids:
                    self._audit(
                        "incident-closed",
                        now,
                        unit=unit_name,
                        reason="manual reset",
                        incident_id=closed.id,
                        closed_at=closed.closed_at,
                    )

        # Rotate the audit log: archive the now-closed history and start a
        # fresh file, so list-incidents shows no residual incidents while the
        # archived trail is kept for forensics.
        rotate_audit_log(audit_path)

        self._status_tracker.reset()
        self.unit.status = ActiveStatus(self._ready_message())
        logger.info("status state cleared")
        event.set_results({"result": "status state cleared"})

    def _on_action_list_incidents(self, event):
        """Return incidents recorded in the audit log, newest first.

        Correlates ``incident-start`` / ``report-generated`` /
        ``incident-closed`` events by incident id, with an optional ``unit``
        filter. Tolerates a missing or malformed log. The result is a JSON
        array under ``result`` because Juju action results are a flat map of
        scalars.
        """
        unit = event.params.get("unit", "").strip()
        incidents = list_incidents(self.model.config.get("audit-log-path", ""), unit=unit)
        event.set_results({"result": json.dumps(incidents, indent=2)})
