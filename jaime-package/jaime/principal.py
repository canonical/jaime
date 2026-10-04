"""Principal unit status tracking for Jaime."""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_DEFAULT_STATE_PATH = "/var/lib/jaime/status-state.json"


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
    ``run_suggest`` or ``run_act`` invocation. The ``cost_usd`` field is
    absent when the provider does not report cost (e.g. Gemini).

    The per-model breakdown returned by ``show-usage`` is computed on the
    fly by ``JaimeCharm._summarise_usage`` and is not stored here.

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

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("could not load status state from %s: %s", self._path, e)
            return {}

    def _save(self) -> None:
        try:
            dir_path = os.path.dirname(self._path)
            os.makedirs(dir_path, exist_ok=True)
            # Write to a temp file in the same directory then rename atomically
            # to avoid leaving a truncated state file if the process is killed.
            with tempfile.NamedTemporaryFile(
                "w", dir=dir_path, delete=False, suffix=".tmp"
            ) as tmp:
                json.dump(self._state, tmp)
                tmp_path = tmp.name
            os.rename(tmp_path, self._path)
        except Exception as e:
            logger.warning("could not save status state to %s: %s", self._path, e)

    def observe(self, unit: str, status: str, since: str,
                watched: bool = True, message: str = "") -> int:
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
        if new_episode:
            entry["increment"] = 1
            entry["unhealthy_since"] = since if watched else None
        else:
            entry["increment"] = previous.get("increment", 0) + 1
            entry["unhealthy_since"] = previous.get("unhealthy_since")
            entry["incident"] = previous.get("incident")
            entry["last_reported"] = previous.get("last_reported")

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

    def record_usage(self, unit: str, usage_entry: dict) -> None:
        """Append a token usage entry to the usage log for a unit."""
        if unit in self._state:
            self._state[unit].setdefault("usage_log", []).append(usage_entry)
            self._save()

    def usage_log(self, unit: str) -> list[dict]:
        """Return all usage log entries for a unit."""
        return self._state.get(unit, {}).get("usage_log", [])

    def all_usage_log(self) -> list[dict]:
        """Return all usage log entries across all units."""
        entries = []
        for entry in self._state.values():
            entries.extend(entry.get("usage_log", []))
        return entries

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
