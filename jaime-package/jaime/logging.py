"""Structured JSONL audit logger for Jaime.

All incident lifecycle events are appended to a single append-only JSONL file.
Each line is a self-contained JSON object. The file is never truncated by Jaime:
once it reaches ``MAX_AUDIT_BYTES`` it is rotated to numbered archives
(``events.jsonl.1`` is the newest), of which ``MAX_AUDIT_ARCHIVES`` are kept.
``list_incidents`` reads the archives together with the live file.
"""

import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_AUDIT_LOG_PATH = "/var/log/jaime/events.jsonl"

# Size-based rotation bound: about (1 + MAX_AUDIT_ARCHIVES) * MAX_AUDIT_BYTES.
MAX_AUDIT_BYTES = 10 * 1024 * 1024
MAX_AUDIT_ARCHIVES = 5


def _archive_paths(path: str) -> list[str]:
    """The numbered size archives of ``path``, newest first."""
    return [f"{path}.{n}" for n in range(1, MAX_AUDIT_ARCHIVES + 1)]


def _rotate_by_size(path: str) -> None:
    """Shift the numbered archives and move ``path`` to ``path.1`` once full."""
    try:
        if os.path.getsize(path) < MAX_AUDIT_BYTES:
            return
    except OSError:
        return
    archives = _archive_paths(path)
    for older, newer in zip(reversed(archives), reversed([path] + archives[:-1])):
        if os.path.exists(newer):
            os.replace(newer, older)
    logger.info("audit log %s rotated by size", path)


def write_event(event: dict, audit_log_path: str = "") -> None:
    """Append a structured event to the JSONL audit log.

    Falls back to _DEFAULT_AUDIT_LOG_PATH if audit_log_path is empty. Rotates
    the log first when it has reached ``MAX_AUDIT_BYTES``.
    Does not raise on failure — logs a warning instead.
    """
    path = audit_log_path or _DEFAULT_AUDIT_LOG_PATH
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _rotate_by_size(path)
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logger.warning("could not write audit event to %s: %s", path, e)


def rotate_audit_log(audit_log_path: str = "") -> str | None:
    """Rotate the audit log: atomically move the current file aside and start fresh.

    Returns the archived file path, or None if there was nothing to rotate
    (missing file or otherwise unusable). The archived copy keeps the full
    history, the original path is recreated empty, and a subsequent
    ``write_event`` appends to the fresh file. The numbered size archives are
    moved aside with it (``<archive>.<n>``), so ``list_incidents`` no longer
    reads them.

    ``reset`` uses this so ``list-incidents`` shows no residual history while
    the audit trail is retained for forensics.
    """
    path = audit_log_path or _DEFAULT_AUDIT_LOG_PATH
    try:
        if not os.path.exists(path):
            return None
        archive = f"{path}.{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        os.rename(path, archive)
        for n, numbered in enumerate(_archive_paths(path), start=1):
            if os.path.exists(numbered):
                os.rename(numbered, f"{archive}.{n}")
        # Recreate the configured path as an empty file so the audit log
        # always exists at its documented location.
        with open(path, "w"):
            pass
        logger.info("audit log rotated to %s", archive)
        return archive
    except Exception as e:
        logger.warning("could not rotate audit log %s: %s", path, e)
        return None


def list_incidents(audit_log_path: str = "", unit: str = "") -> list[dict]:
    """Correlate events.jsonl into per-incident records, newest first.

    Each incident id is the correlation key across three event kinds:

    - ``incident-start``     opened_at, unit, workload, status_message, first_seen, status_since
    - ``report-generated``   report_path
    - ``incident-closed``    closed_at

    Incidents that never received an ``incident-closed`` row — for example
    events logged before closure was written to the audit log, or incidents
    still open — are reported with ``status: open``.

    Tolerates a missing or partially malformed log: a missing file yields an
    empty list and unparsable lines are skipped rather than aborting the read.
    ``unit`` filters the result to incidents whose events carry that unit name.
    The numbered size archives are read first, oldest first, then the live file.
    """
    path = audit_log_path or _DEFAULT_AUDIT_LOG_PATH
    by_id: dict[str, dict] = {}
    files = [p for p in reversed(_archive_paths(path)) if os.path.exists(p)] + [path]
    for file_path in files:
        _read_incident_events(file_path, unit, by_id)

    records = list(by_id.values())
    for record in records:
        record["status"] = "closed" if record["closed_at"] else "open"
    records.sort(key=lambda r: r["opened_at"] or "", reverse=True)
    return records


def _read_incident_events(path: str, unit: str, by_id: dict[str, dict]) -> None:
    """Fold one audit file's incident events into ``by_id``; skip it if unreadable."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                incident_id = event.get("incident_id")
                if not incident_id:
                    continue
                if unit and event.get("unit") != unit:
                    continue
                record = by_id.setdefault(
                    incident_id,
                    {
                        "incident_id": incident_id,
                        "unit": event.get("unit", ""),
                        "workload": "",
                        "status_message": event.get("status_message", ""),
                        "first_seen": "",
                        "status_since": "",
                        "opened_at": "",
                        "report_path": "",
                        "closed_at": None,
                    },
                )
                kind = event.get("event")
                if kind == "incident-start":
                    record["workload"] = event.get("workload", "")
                    record["status_message"] = event.get("status_message", "")
                    record["first_seen"] = event.get("first_seen", "")
                    record["status_since"] = event.get("status_since", "")
                    record["opened_at"] = event.get("timestamp", "")
                elif kind == "report-generated":
                    record["report_path"] = event.get("report_path", "")
                elif kind == "incident-closed":
                    record["closed_at"] = event.get("closed_at") or event.get("timestamp")
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("could not read audit log %s: %s", path, e)
