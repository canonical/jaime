"""Principal unit status tracking for Jaime."""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = "/var/lib/jaime/status-state.json"

# Usage entries kept per unit. Older entries are folded into per-model totals
# (``usage_folded``) so the state file stays bounded and totals stay exact.
MAX_USAGE_ENTRIES = 200

# Per-model counters kept by a folded usage total.
_USAGE_COUNTERS = ("prompt_tokens", "completion_tokens", "total_tokens")


class StatusTracker:
    """Persist per-unit status observations across hook invocations.

    State file schema::

        {
            "postgresql/0": {
                "status": "blocked",
                "since": "2026-07-14T09:37:54+00:00",
                "message": "Please initialize the database or provide credentials",
                "unhealthy_since": "2026-07-14T09:37:54+00:00",
                "increment": 3,
                "incident": {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "opened_at": "2026-07-14T09:39:39+00:00"
                },
                "last_reported": "2026-07-14T09:39:39+00:00",
                "usage_log": [
                    {
                        "incident_id": "550e8400-...",
                        "timestamp": "2026-07-14T09:40:00+00:00",
                        "model": "deepseek/deepseek-chat",
                        "prompt_tokens": 1243,
                        "completion_tokens": 387,
                        "total_tokens": 1630,
                        "cost_usd": 0.000524
                    }
                ]
            }
        }

    Each ``usage_log`` entry is one raw LLM call record — one entry per
    ``run_suggest`` or ``run_ask`` invocation. The ``cost_usd`` field is
    absent when the provider does not report cost (e.g. Gemini). Only the
    newest ``MAX_USAGE_ENTRIES`` are kept; older ones are folded into
    ``usage_folded``, a per-model total of calls, tokens and cost.

    ``ai_error`` is the reason the last automatic AI call for the open
    incident failed, kept so the unit stays blocked on it while the incident
    is in cooldown. It is cleared by a successful turn or a new episode.

    The per-model breakdown returned by ``show-usage`` is computed on the
    fly by ``jaime.actions.summarise_usage`` and is not stored here.

    ``since`` is Juju's own "status last set" timestamp and is recorded for
    reporting only. ``unhealthy_since`` is Jaime's anchor for how long the
    unit has been unhealthy: it is set when the unit enters a watched status
    and held steady until the unit recovers. ``message`` is the workload
    status message ("reason") shown in ``juju status``; it may be empty when
    the observing path is the machine charm's goal-state hook tool, which
    carries no message.

    An episode is a continuous run of watched (or unwatched) observations.
    The increment, incident, and last_reported reset when the unit crosses
    that boundary — not when Juju bumps ``since`` and not when a workload
    flaps between two watched statuses. usage_log is preserved across
    episodes and never cleared.
    """

    def __init__(self, state_path: str = _DEFAULT_STATE_PATH):
        self._path = state_path
        self._state: dict = self._load()

    @property
    def path(self) -> str:
        """Path of the state file; sibling files live next to it."""
        return self._path

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("could not load status state from %s: %s", self._path, e)
            return {}

    def _save(self) -> None:
        try:
            dir_path = os.path.dirname(self._path)
            os.makedirs(dir_path, exist_ok=True)
            # Write to a temp file in the same directory, flush it to disk, then
            # rename atomically, so neither a killed process nor a power loss
            # leaves a truncated or empty state file.
            with tempfile.NamedTemporaryFile("w", dir=dir_path, delete=False, suffix=".tmp") as tmp:
                json.dump(self._state, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.rename(tmp_path, self._path)
        except (OSError, TypeError, ValueError) as e:
            logger.warning("could not save status state to %s: %s", self._path, e)

    def observe(self, unit: str, status: str, since: str, watched: bool = True, message: str = "") -> int:
        """Record a status observation for a unit.

        ``watched`` says whether ``status`` is one of the statuses that open an
        incident. Crossing that boundary starts a new episode: the increment
        resets to 1 and incident/last_reported are cleared. Staying on the same
        side of it continues the episode, so a workload that flaps between two
        watched statuses — or one that re-sets the same status with a new
        message, bumping Juju's ``since`` — keeps a single incident and a
        single unhealthy timer.

        ``message`` is the workload status message (the "reason" shown in
        ``juju status``, e.g. "Please initialize OpenBao or integrate with an
        auto-unseal provider"). The machine charm's goal-state path has no
        message, so it may be empty; the controller-API paths (k8s and the
        machine charm's opt-in co-located watch) carry it.

        ``unhealthy_since`` is set from ``since`` when a watched episode starts
        and preserved for its duration. It is cleared on recovery.

        Returns the current increment.
        """
        previous = self._state.get(unit)
        if previous is None:
            new_episode = True
            previous = {}
        else:
            was_watched = previous.get("unhealthy_since") is not None
            new_episode = was_watched != watched

        entry = {
            "status": status,
            "since": since,
            "message": message,
            "usage_log": previous.get("usage_log", []),
        }
        if previous.get("usage_folded"):
            entry["usage_folded"] = previous["usage_folded"]
        if new_episode:
            entry["increment"] = 1
            entry["unhealthy_since"] = since if watched else None
        else:
            entry["increment"] = previous.get("increment", 0) + 1
            entry["unhealthy_since"] = previous.get("unhealthy_since")
            entry["incident"] = previous.get("incident")
            entry["last_reported"] = previous.get("last_reported")
            if previous.get("ai_error"):
                entry["ai_error"] = previous["ai_error"]

        self._state[unit] = entry
        # Always persist: each hook is a fresh process, so an in-memory-only
        # increment would be reloaded from the last written value and never
        # advance past 2.
        self._save()
        return entry["increment"]

    def unhealthy_since(self, unit: str) -> str | None:
        """Return when this unit entered its current watched episode, or None."""
        return self._state.get(unit, {}).get("unhealthy_since")

    def record_reported(self, unit: str, timestamp: str, incident_dict: dict) -> None:
        """Record that an incident was opened and reported for this unit."""
        if unit in self._state:
            self._state[unit]["last_reported"] = timestamp
            self._state[unit]["incident"] = incident_dict
            self._save()

    def close_incident(self, unit: str, closed_incident_dict: dict) -> None:
        """Record the closed incident for a unit."""
        if unit in self._state:
            self._state[unit]["incident"] = closed_incident_dict
            self._save()

    def update_incident(self, unit: str, incident_dict: dict) -> None:
        """Update the stored incident dict (e.g. to attach a suggestion)."""
        if unit in self._state:
            self._state[unit]["incident"] = incident_dict
            self._save()

    def set_ai_error(self, unit: str, message: str) -> None:
        """Record (or, with an empty message, clear) the last AI failure."""
        if unit not in self._state:
            return
        if message:
            self._state[unit]["ai_error"] = message
        elif not self._state[unit].pop("ai_error", None):
            return
        self._save()

    def ai_error(self, unit: str) -> str:
        """The reason the last automatic AI call for the unit failed, or ''."""
        return self._state.get(unit, {}).get("ai_error", "")

    def record_usage(self, unit: str, usage_entry: dict) -> None:
        """Append a token usage entry, folding the oldest beyond the cap."""
        if unit not in self._state:
            return
        state = self._state[unit]
        log = state.setdefault("usage_log", [])
        log.append(usage_entry)
        if len(log) > MAX_USAGE_ENTRIES:
            overflow = log[: len(log) - MAX_USAGE_ENTRIES]
            del log[: len(overflow)]
            folded = state.setdefault("usage_folded", {})
            for old in overflow:
                _fold_usage(folded, old)
        self._save()

    def usage_log(self, unit: str) -> list[dict]:
        """Return the retained usage log entries for a unit."""
        return self._state.get(unit, {}).get("usage_log", [])

    def all_usage_log(self) -> list[dict]:
        """Return the retained usage log entries across all units."""
        entries = []
        for entry in self._state.values():
            entries.extend(entry.get("usage_log", []))
        return entries

    def folded_usage(self) -> dict[str, dict]:
        """Per-model totals of the usage entries folded out of every unit's log."""
        total: dict[str, dict] = {}
        for entry in self._state.values():
            for model, counts in (entry.get("usage_folded") or {}).items():
                _merge_folded(total, model, counts)
        return total

    def units(self) -> list[str]:
        """Names of every tracked unit."""
        return list(self._state)

    def entries(self) -> list[tuple[str, dict]]:
        """``(unit, state entry)`` pairs for every tracked unit."""
        return list(self._state.items())

    def incidents(self, incident_id: str = "", include_closed: bool = False) -> list[tuple[str, dict, dict]]:
        """``(unit, state entry, incident)`` triples for matching incidents.

        With ``incident_id`` the incident with that id is returned, open or
        closed. Without it, only open incidents are returned unless
        ``include_closed`` is set.
        """
        found = []
        for unit, entry in self._state.items():
            incident = entry.get("incident")
            if not incident:
                continue
            if incident_id:
                if incident["id"] != incident_id:
                    continue
            elif incident.get("closed_at") is not None and not include_closed:
                continue
            found.append((unit, entry, incident))
        return found

    def has_open_incident(self, unit: str) -> bool:
        """Return True if there is an open (not yet closed) incident for a unit."""
        incident = self._state.get(unit, {}).get("incident")
        if not incident:
            return False
        return incident.get("closed_at") is None

    def last_reported(self, unit: str) -> str | None:
        """Return the ISO timestamp of the last reported incident, or None."""
        return self._state.get(unit, {}).get("last_reported")

    def current_incident(self, unit: str) -> dict | None:
        """Return the current incident dict for a unit, or None."""
        return self._state.get(unit, {}).get("incident")

    def reset(self) -> None:
        """Clear all tracked state and persist the empty state file."""
        self._state = {}
        self._save()


def _fold_usage(folded: dict, entry: dict) -> None:
    """Add one usage entry to a per-model folded total."""
    _merge_folded(
        folded,
        entry.get("model") or "unknown",
        {"calls": 1, **{k: entry.get(k, 0) for k in _USAGE_COUNTERS}, "cost_usd": entry.get("cost_usd")},
    )


def _merge_folded(folded: dict, model: str, counts: dict) -> None:
    """Add a per-model count record into ``folded[model]``."""
    target = folded.setdefault(model, {"calls": 0, **{k: 0 for k in _USAGE_COUNTERS}, "cost_usd": None})
    target["calls"] += counts.get("calls", 0)
    for key in _USAGE_COUNTERS:
        target[key] += counts.get(key, 0)
    if counts.get("cost_usd") is not None:
        target["cost_usd"] = (target["cost_usd"] or 0.0) + counts["cost_usd"]
