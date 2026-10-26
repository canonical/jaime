"""Juju action handlers shared by both Jaime charms.

``ActionsMixin`` holds every ``_on_action_*`` handler. It reads charm state
(``self.model``, ``self._status_tracker``) and calls back into ``CoreMixin``
for the AI pipeline (``_run_mode_logic``, ``_store_turn``,
``_store_suggestion``) and the substrate hooks, so it is only meant to be
mixed into ``CoreMixin``.
"""

import datetime
import hashlib
import json
import logging
import os

from ops.model import ActiveStatus

from jaime.incident import Incident, Turn
from jaime.logging import list_incidents, rotate_audit_log
from jaime.modes import Mode
from jaime.redact import redact_text
from jaime.report import generate_report

logger = logging.getLogger(__name__)


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
        incident_id = event.params.get("incident-id", "").strip()
        all_entries = self._status_tracker.all_usage_log()

        if not all_entries:
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
            summary = summarise_usage(all_entries)
            summary["total_incidents"] = len({e.get("incident_id") for e in all_entries})
            summary["total_calls"] = len(all_entries)

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

        if Mode(self.model.config.get("mode", Mode.OBSERVE)) == Mode.OBSERVE:
            event.fail("no suggestion available: mode is 'observe'")
            return

        report_content, error = self._read_report(inc)
        if error:
            event.fail(error)
            return

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
        results.update(self._usage_results(suggestion.usage))
        event.set_results(results)

    def _cached_suggestion(self, inc: dict, additional_context: str, context_hash: str) -> dict | None:
        """The stored one-shot suggestion if it still answers this request.

        Ask turns answer a specific question, so only the latest turn with an
        empty question (or the legacy single suggestion) can serve as the
        cache. It is stale when the configured model changed, or when a
        different ``additional-context`` was supplied.
        """
        turns = inc.get("turns") or []
        oneshot = next((t for t in reversed(turns) if not t.get("question")), None)
        suggestion = oneshot or (None if turns else inc.get("suggestion"))
        if not suggestion:
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
        """Read an incident's stored report: ``(content, error)``."""
        report_dir = self.model.config.get("report-dir", "") or "/var/log/jaime/reports"
        report_path = os.path.join(report_dir, f"{inc['id']}.md")
        if not os.path.exists(report_path):
            return "", f"report file not found at {report_path}"
        with open(report_path) as f:
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

        mode = Mode(self.model.config.get("mode", Mode.OBSERVE))
        if mode != Mode.SUGGEST:
            event.fail(f"ask requires mode=suggest (current mode is '{mode.value}')")
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
                        "description": turn.get("description", ""),
                    }
                )
        event.set_results({"result": json.dumps(records)})

    def _on_action_generate_report(self, event):
        for unit_name, entry, inc in self._status_tracker.incidents():
            since_iso = entry.get("unhealthy_since") or entry.get("since", "")
            context = self._collect_report_context(unit_name, since_iso)
            report_path = generate_report(
                incident_id=inc["id"],
                unit_name=unit_name,
                workload=entry.get("status", "unknown"),
                first_seen=since_iso,
                context=context,
                report_dir=self.model.config.get("report-dir", ""),
                status_message=entry.get("message", ""),
            )
            event.set_results(
                {
                    "incident-id": inc["id"],
                    "report-path": report_path,
                }
            )
            return
        event.fail("no open incident found")

    def _on_action_reset(self, event):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit_path = self.model.config.get("audit-log-path", "")

        # Close every incident still open in the audit log as well as the ones
        # the tracker carries. events.jsonl is append-only and remembers every
        # incident ever opened, while status-state.json keeps only the latest
        # per unit - so a reset that closes only the tracked incidents would
        # leave older audit rows reported as open by list-incidents forever.
        # Append a closure row per open incident; we never rewrite history.
        open_records = list_incidents(audit_path)
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
