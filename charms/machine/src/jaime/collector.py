"""Context collection for Jaime incidents.

Collects bounded diagnostics from the local machine without modifying
any state. All collection is read-only and bounded by time and line count.
"""

import datetime
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess

from jaime.logutils import cap_lines as _cap_lines
from jaime.logutils import deduplicate_lines as _deduplicate_lines
from jaime.logutils import tail_lines as _tail_lines

logger = logging.getLogger(__name__)

_DEFAULT_LOG_WINDOW_MINUTES = 120
_DEFAULT_MAX_LINES = 500
_JUJU_LOG_DIR = "/var/log/juju"

# Runaway guards, not relevance judgements: each section is capped so a large
# host cannot produce an unbounded report. Relevance is decided at prompt
# projection time (4.10). See ARCHITECTURE.md, "Context evidence and prompt
# projection".
_CAP_FIREWALL = 100
_CAP_SYSTEMD_FAILED = 50
_CAP_SYSTEMD_DETAIL = 50
_CAP_SS = 200
_CAP_SNAP_LOGS = 3          # failed snaps inspected
_CAP_SNAP_LINES = 100       # lines per failed snap
_CAP_SNAP_CHANGES = 20
_CAP_DISK = 100
_CAP_MEMORY = 50
_CAP_CHARM_CONFIG = 200
_CAP_HEALTH_OUTPUT = 100

# The diagnostics plan is operator- or AI-authored, so its item counts are
# capped too; otherwise a large plan could multiply every per-item limit.
_PLAN_ITEM_CAPS = {
    "log_files": 10,
    "processes": 25,
    "systemd_units": 25,
    "network_ports": 25,
    "env_variables": 25,
    "health_commands": 5,
}


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout
    except Exception as e:
        logger.debug("collection command %s failed: %s", cmd, e)
        return ""


def collect_tracing_events(
    unit_name: str,
    from_time: datetime.datetime | None = None,
    max_events: int = 50,
    buffer_minutes: int = 5,
) -> list[dict]:
    """Read recent Ops framework events from the principal unit's tracing DB.

    The tracing DB (``.tracing-data.db``) is written by the Ops framework's
    built-in OpenTelemetry tracing layer. It only exists for charms that use
    the Ops framework with tracing enabled. Returns an empty list if the file
    is absent, unreadable, or not an Ops tracing DB.

    Each returned dict has:
      - ``timestamp``: ISO 8601 UTC
      - ``event``: Ops event name (e.g. ``UpdateStatusEvent``)
      - ``kind``: hook kind (e.g. ``update_status``)
      - ``exception_type``: exception class name if the hook failed, else ``""``
      - ``exception_message``: first line of exception message, else ``""``
    """
    tag = "unit-" + unit_name.replace("/", "-")
    db_path = f"/var/lib/juju/agents/{tag}/charm/.tracing-data.db"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except Exception as e:
        logger.debug("tracing DB not accessible for %s: %s", unit_name, e)
        return []

    if from_time is not None:
        from_time = from_time - datetime.timedelta(minutes=buffer_minutes)

    events = []
    try:
        rows = conn.execute(
            "SELECT data FROM tracing ORDER BY id DESC LIMIT 500"
        ).fetchall()

        for (data,) in rows:
            try:
                payload = json.loads(data)
            except Exception:
                continue

            for rs in payload.get("resourceSpans", []):
                for ss in rs.get("scopeSpans", []):
                    for span in ss.get("spans", []):
                        if span.get("name") != "ops.main":
                            continue

                        ts_ns = int(span.get("startTimeUnixNano", 0))
                        ts = datetime.datetime.fromtimestamp(
                            ts_ns / 1e9, tz=datetime.timezone.utc
                        )

                        if from_time and ts < from_time:
                            continue

                        for evt in span.get("events", []):
                            evt_name = evt.get("name", "")
                            if evt_name in (
                                "PreCommitEvent", "CommitEvent",
                                "CollectAppStatusEvent", "CollectUnitStatusEvent",
                                "UpdateStatusEvent",
                            ):
                                continue

                            attrs = {
                                a["key"]: list(a["value"].values())[0]
                                for a in evt.get("attributes", [])
                            }

                            if evt_name == "exception":
                                # Attach exception to the previous event if any
                                if events:
                                    events[-1]["exception_type"] = attrs.get(
                                        "exception.type", ""
                                    )
                                    msg = attrs.get("exception.message", "")
                                    events[-1]["exception_message"] = msg.splitlines()[0] if msg else ""
                                continue

                            events.append({
                                "timestamp": ts.isoformat(),
                                "event": evt_name,
                                "kind": attrs.get("kind", ""),
                                "exception_type": "",
                                "exception_message": "",
                            })

        conn.close()
    except Exception as e:
        logger.debug("could not read tracing DB for %s: %s", unit_name, e)
        return []

    # Sort ascending by timestamp, return most recent max_events
    events.sort(key=lambda e: e["timestamp"])
    return events[-max_events:]


def collect_unit_logs(
    unit_name: str,
    log_window_minutes: int = _DEFAULT_LOG_WINDOW_MINUTES,
    max_lines: int = _DEFAULT_MAX_LINES,
    from_time: datetime.datetime | None = None,
    buffer_minutes: int = 5,
    context_window: int = 10,
) -> list[str]:
    """Read recent lines from the principal unit's Juju log file.

    The log window is anchored to ``from_time`` when provided (typically the
    incident's ``first_seen`` timestamp).  Lines before
    ``from_time - buffer_minutes`` are excluded.  ``log_window_minutes`` still
    acts as a hard cap so very old incidents don't pull unbounded history.

    When ``from_time`` is None the window falls back to
    ``now - log_window_minutes`` (the original behaviour).

    Lines are filtered to include only those matching ``(error|warning)``
    (case-insensitive).  All matching lines are included, plus a context
    window of ``context_window`` lines before and after the **last**
    chronological match.  If no matches are found, all time-bounded lines
    are returned as a fallback.
    """
    tag = "unit-" + unit_name.replace("/", "-")
    log_path = os.path.join(_JUJU_LOG_DIR, f"{tag}.log")

    try:
        with open(log_path) as f:
            raw_lines = f.readlines()
    except FileNotFoundError:
        logger.debug("log file not found: %s", log_path)
        return []
    except Exception as e:
        logger.debug("could not read %s: %s", log_path, e)
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    earliest_allowed = now - datetime.timedelta(minutes=log_window_minutes)

    if from_time is not None:
        cutoff = from_time - datetime.timedelta(minutes=buffer_minutes)
    else:
        cutoff = earliest_allowed

    recent = []
    for line in raw_lines:
        try:
            ts_str = line[:19]
            ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
            if ts >= cutoff:
                recent.append(line.rstrip())
        except ValueError:
            if recent:
                recent.append(line.rstrip())

    # Find indices of lines where the log level is ERROR or WARNING
    matched_indices = [
        i for i, line in enumerate(recent)
        if re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (ERROR|WARNING)\s", line)
    ]

    if not matched_indices:
        return _cap_lines(
            _deduplicate_lines(_tail_lines("\n".join(recent), max_lines)), max_lines
        )

    # Build set of indices to include: all matched lines + context around last
    include_indices = set(matched_indices)
    last_idx = matched_indices[-1]
    window_start = max(0, last_idx - context_window)
    window_end = min(len(recent) - 1, last_idx + context_window)
    for j in range(window_start, window_end + 1):
        include_indices.add(j)

    result = [recent[i] for i in sorted(include_indices)]
    result = result[-max_lines:] if len(result) > max_lines else result
    return _cap_lines(_deduplicate_lines(result), max_lines)


def collect_systemd_failed(max_lines: int = _CAP_SYSTEMD_FAILED) -> list[str]:
    output = _run(["systemctl", "--failed", "--no-legend", "--plain"])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return _cap_lines(lines, max_lines)


def collect_disk_usage(max_lines: int = _CAP_DISK) -> list[str]:
    output = _run(["df", "-h", "--output=source,size,used,avail,pcent,target"])
    return _cap_lines([line.rstrip() for line in output.splitlines() if line.strip()], max_lines)


def collect_memory_summary(max_lines: int = _CAP_MEMORY) -> list[str]:
    output = _run(["free", "-h"])
    return _cap_lines([line.rstrip() for line in output.splitlines() if line.strip()], max_lines)


# ---------------------------------------------------------------------------
# Plan-driven collection helpers
# ---------------------------------------------------------------------------


def _collect_log_files(plan_log_files: list[dict], max_lines: int) -> list[dict]:
    results = []
    remaining = max_lines  # total budget across all files, not per file
    for lf in plan_log_files:
        path = lf.get("path", "")
        priority = lf.get("priority", "medium")
        description = lf.get("description", "")
        try:
            with open(path) as f:
                lines = _cap_lines(_deduplicate_lines(_tail_lines(f.read(), max_lines)), max_lines)
            if remaining <= 0:
                lines = []
            elif len(lines) > remaining:
                lines = lines[-remaining:]
            remaining -= len(lines)
            results.append({
                "path": path,
                "priority": priority,
                "description": description,
                "status": "available",
                "lines": lines,
            })
        except FileNotFoundError:
            results.append({
                "path": path,
                "priority": priority,
                "description": description,
                "status": "not_found",
                "lines": [],
            })
        except Exception as e:
            logger.debug("could not read log file %s: %s", path, e)
            results.append({
                "path": path,
                "priority": priority,
                "description": description,
                "status": "error",
                "lines": [],
            })
    return results


def _collect_processes(plan_processes: list[dict]) -> list[dict]:
    results = []
    for proc in plan_processes:
        name = proc.get("name", "")
        output = _run(["pgrep", "-f", name])
        count = len([line for line in output.splitlines() if line.strip()]) if output else 0
        expected_min = proc.get("expected_min_count", 1)
        expected_max = proc.get("expected_max_count", 1)
        if count < expected_min:
            status = "too_few"
        elif count > expected_max:
            status = "too_many"
        else:
            status = "ok"
        results.append({
            "name": name,
            "expected_min_count": expected_min,
            "expected_max_count": expected_max,
            "running_count": count,
            "status": status,
        })
    return results


def _systemd_detail(unit: str) -> dict:
    """Compact, high-signal service state: restart count and exit status."""
    output = _run([
        "systemctl", "show",
        "-p", "ActiveState,SubState,NRestarts,ExecMainStatus",
        unit,
    ])
    data = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    return {
        "unit": unit,
        "status": data.get("ActiveState", "unknown") or "unknown",
        "substate": data.get("SubState", ""),
        "restarts": data.get("NRestarts", ""),
        "exec_main_status": data.get("ExecMainStatus", ""),
    }


def _collect_systemd_units(plan_units: list[str]) -> list[dict]:
    return [_systemd_detail(unit) for unit in plan_units[:_CAP_SYSTEMD_DETAIL]]


def _collect_systemd_failed_detail(failed_units: list[str]) -> list[dict]:
    return [_systemd_detail(unit) for unit in failed_units[:_CAP_SYSTEMD_DETAIL]]


def _collect_network_ports(plan_ports: list[dict], ss_output: str) -> list[dict]:
    results = []
    for port_def in plan_ports:
        port = port_def.get("port")
        protocol = port_def.get("protocol", "tcp")
        # Match the port anchored to a colon and followed by a non-digit to
        # avoid false positives (e.g. port 80 matching 8080 or 8000).
        listening = bool(re.search(rf":{port}(?!\d)", ss_output)) if port else False
        results.append({
            "port": port,
            "protocol": protocol,
            "status": "listening" if listening else "not_listening",
        })
    return results


def _collect_env_variables(plan_vars: list[str]) -> list[dict]:
    """Report only whether each variable is set, never its value.

    This reads Jaime's own hook environment, not the workload's, so a value
    would be both misleading and a secret-exposure risk.
    """
    results = []
    for var in plan_vars:
        results.append({
            "name": var,
            "status": "set" if var in os.environ else "unset",
        })
    return results


def _collect_health_commands(plan_health_commands: list[dict],
                             max_lines: int = _CAP_HEALTH_OUTPUT) -> list[dict]:
    results = []
    for cmd_def in plan_health_commands:
        command = cmd_def.get("command", "")
        timeout = cmd_def.get("timeout_seconds", 30)
        argv = shlex.split(command)
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
            )
            results.append({
                "command": command,
                "timeout_seconds": timeout,
                "returncode": result.returncode,
                "stdout": "\n".join(_cap_lines(result.stdout.splitlines(), max_lines)),
                "stderr": "\n".join(_cap_lines(result.stderr.splitlines(), max_lines)),
            })
        except subprocess.TimeoutExpired:
            results.append({
                "command": command,
                "timeout_seconds": timeout,
                "returncode": -1,
                "stdout": "",
                "stderr": "timed out after {}s".format(timeout),
            })
        except Exception as e:
            logger.debug("health command %s failed: %s", command, e)
            results.append({
                "command": command,
                "timeout_seconds": timeout,
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
            })
    return results


# ---------------------------------------------------------------------------
# Broad fallback collection helpers (used when no plan or empty plan)
# ---------------------------------------------------------------------------


def _collect_broad_processes(max_lines: int = 100) -> list[str]:
    output = _run(["ps", "aux"])
    return _cap_lines(output.splitlines(), max_lines)


def _collect_broad_ports(ss_output: str, max_lines: int) -> list[str]:
    """Listening sockets, derived from the single ``ss`` collection."""
    lines = [line.rstrip() for line in ss_output.splitlines() if "LISTEN" in line]
    return _cap_lines(lines, max_lines)


def collect_ss_connections(max_lines: int = _CAP_SS) -> list[str]:
    """Collect listening and established TCP/UDP connections with process info.

    Hooks run as root, so no ``sudo`` is needed. This is the single ``ss``
    collection for the whole context; the broad-ports section is derived from
    its output rather than invoking ``ss`` again.
    """
    output = _run(["ss", "-antlup"])
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    return _cap_lines(lines, max_lines)


def collect_firewall_rules(max_lines: int = _CAP_FIREWALL) -> dict:
    """Collect IPv4 firewall rules from iptables, ufw, and nftables.

    Returns a dict with keys ``iptables``, ``ufw``, ``nftables`` — each
    is a list of output lines, or absent if the command failed or the
    tool is not installed. Each table is independently capped.
    """
    result = {}

    iptables_out = _run(["iptables", "-L", "-n"])
    if iptables_out:
        lines = [line.rstrip() for line in iptables_out.splitlines() if line.strip()]
        result["iptables"] = _cap_lines(lines, max_lines)

    ufw_out = _run(["ufw", "status", "verbose"])
    if ufw_out:
        lines = [line.rstrip() for line in ufw_out.splitlines() if line.strip()]
        result["ufw"] = _cap_lines(lines, max_lines)

    nft_out = _run(["nft", "list", "ruleset", "ip"])
    if nft_out:
        lines = [line.rstrip() for line in nft_out.splitlines() if line.strip()]
        result["nftables"] = _cap_lines(lines, max_lines)

    return result


def collect_charm_config(unit_name: str, max_lines: int = _CAP_CHARM_CONFIG) -> dict:
    """Read the principal charm's config.yaml and actions.yaml.

    Returns a dict with keys ``config_yaml`` and ``actions_yaml`` containing
    the raw file content, or ``""`` if the file is missing/unreadable.

    Bounded by line count only, never by truncating a line: the report parses
    this YAML, and a truncated line would break the parse and drop the section.
    """
    tag = "unit-" + unit_name.replace("/", "-")
    charm_dir = f"/var/lib/juju/agents/{tag}/charm"
    result = {}
    for name in ("config.yaml", "actions.yaml"):
        path = os.path.join(charm_dir, name)
        try:
            with open(path) as f:
                raw = f.read()
            lines = raw.splitlines()
            if len(lines) > max_lines:
                raw = "\n".join(lines[-max_lines:])
            result[name.replace(".yaml", "_yaml")] = raw
        except Exception as e:
            logger.debug("could not read %s for %s: %s", path, unit_name, e)
            result[name.replace(".yaml", "_yaml")] = ""
    return result


# ---------------------------------------------------------------------------
# Snap diagnostics
# ---------------------------------------------------------------------------
#
# Snap status is only worth a section when something has actually failed, so
# the whole snap context is omitted on healthy hosts and hosts without snapd.
# Logs are collected only for failed snaps, from the failing service, centred
# on the latest error/warning rather than the last N raw lines.

def _failed_snap_services(output: str) -> list[tuple[str, str]]:
    """Return (snap, service) pairs for snap services that are not active."""
    failed = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or "Startup" in line:
            continue
        service, _startup, current = parts[0], parts[1], parts[2]
        if current != "active" and "." in service:
            failed.append((service.split(".", 1)[0], service))
    return failed


def _failed_snap_changes(output: str) -> list[str]:
    """Return raw rows for snap changes that ended in Error or Undone."""
    failed = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "ID":
            continue
        if parts[1] in ("Error", "Undone"):
            failed.append(line.rstrip())
    return failed


def _error_window(lines: list[str], context_window: int, max_lines: int) -> list[str]:
    """Keep the latest error/warning match with a context window around it.

    ``snap logs`` output has no syslog-level column, so the match is a
    case-insensitive text search, as elsewhere. Falls back to the tail when
    nothing matches.
    """
    matches = [
        i for i, line in enumerate(lines)
        if re.search(r"error|warning", line, re.IGNORECASE)
    ]
    if not matches:
        return _cap_lines(lines, max_lines)
    last = matches[-1]
    lo = max(0, last - context_window)
    hi = min(len(lines), last + context_window + 1)
    return _cap_lines(lines[lo:hi], max_lines)


def collect_snap_context(max_lines: int = _DEFAULT_MAX_LINES) -> dict:
    """Collect snap status and failed-snap logs, or {} when nothing failed."""
    if shutil.which("snap") is None:
        return {}

    packages_out = _run(["snap", "list"])
    services_out = _run(["snap", "services"])
    changes_out = _run(["snap", "changes"])

    failed_services = _failed_snap_services(services_out)
    failed_changes = _failed_snap_changes(changes_out)
    if not failed_services and not failed_changes:
        return {}

    fetch_cap = max(max_lines * 4, 200)
    logs = {}
    for snap, service in failed_services[:_CAP_SNAP_LOGS]:
        output = _run(
            ["snap", "logs", "-n", str(fetch_cap), f"{snap}.{service}"],
            timeout=15,
        )
        if output:
            logs[snap] = _error_window(
                output.splitlines(), 10, min(max_lines, _CAP_SNAP_LINES)
            )

    return {
        "packages": _cap_lines(packages_out.splitlines(), max_lines),
        "services": _cap_lines(services_out.splitlines(), max_lines),
        "failed_changes": _cap_lines(failed_changes, _CAP_SNAP_CHANGES),
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# Main collection entry point
# ---------------------------------------------------------------------------


def _capped(section: list, name: str) -> list:
    """Apply the plan item-count cap for a monitoring-plan section."""
    return section[:_PLAN_ITEM_CAPS.get(name, len(section))]


def collect_context(
    unit_name: str,
    log_window_minutes: int = _DEFAULT_LOG_WINDOW_MINUTES,
    max_lines: int = _DEFAULT_MAX_LINES,
    diagnostics_plan: dict | None = None,
    from_time: datetime.datetime | None = None,
    buffer_minutes: int = 5,
) -> dict:
    """Collect all context for an incident.

    ``from_time`` should be set to the incident's ``first_seen`` datetime so
    that unit logs are anchored to the start of the incident rather than the
    current time, avoiding noise from unrelated prior events.

    If ``diagnostics_plan`` is provided with non-empty sections, collection is
    driven by the plan (only what the plan specifies).  If a section is empty
    or ``diagnostics_plan`` is None, a broad fallback is used for that section.

    Every section is bounded. Caps are runaway guards, not relevance
    judgements: the bounded evidence is kept in the report, and the prompt
    projection (4.10) decides what the model sees.
    """
    # One ss collection for the whole context; the broad network fallback is
    # derived from it rather than invoking ss a second time.
    ss_lines = collect_ss_connections(max_lines)
    ss_text = "\n".join(ss_lines)

    systemd_failed = collect_systemd_failed()

    context = {
        "unit_logs": collect_unit_logs(
            unit_name, log_window_minutes, max_lines,
            from_time=from_time, buffer_minutes=buffer_minutes,
        ),
        "tracing_events": collect_tracing_events(
            unit_name, from_time=from_time, buffer_minutes=buffer_minutes,
        ),
        "charm_config": collect_charm_config(unit_name, max_lines),
        "disk_usage": collect_disk_usage(_CAP_DISK),
        "memory_summary": collect_memory_summary(_CAP_MEMORY),
        "ss_connections": ss_lines,
        "firewall_rules": collect_firewall_rules(_CAP_FIREWALL),
        "systemd_failed": systemd_failed,
        "systemd_failed_detail": _collect_systemd_failed_detail(systemd_failed),
        "snap": collect_snap_context(max_lines),
        "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    plan_results = {}

    if diagnostics_plan is None:
        plan_results["processes"] = {
            "type": "broad",
            "lines": _collect_broad_processes(max_lines),
        }
        plan_results["network_ports"] = {
            "type": "broad",
            "lines": _collect_broad_ports(ss_text, max_lines),
        }
    else:
        mp = diagnostics_plan.get("monitoring_plan", {})

        log_files = _capped(mp.get("log_files", []), "log_files")
        if log_files:
            plan_results["log_files"] = {
                "type": "plan",
                "items": _collect_log_files(log_files, max_lines),
            }

        processes = _capped(mp.get("processes", []), "processes")
        if processes:
            plan_results["processes"] = {
                "type": "plan",
                "items": _collect_processes(processes),
            }
        else:
            plan_results["processes"] = {
                "type": "broad",
                "lines": _collect_broad_processes(max_lines),
            }

        systemd_units = _capped(mp.get("systemd_units", []), "systemd_units")
        if systemd_units:
            plan_results["systemd_units"] = {
                "type": "plan",
                "items": _collect_systemd_units(systemd_units),
            }
        else:
            plan_results["systemd_units"] = {
                "type": "broad",
                "lines": systemd_failed,
            }

        network = mp.get("network", {})
        ports = _capped(network.get("ports", []) if isinstance(network, dict) else [],
                        "network_ports")
        if ports:
            plan_results["network_ports"] = {
                "type": "plan",
                "items": _collect_network_ports(ports, ss_text),
            }
        else:
            plan_results["network_ports"] = {
                "type": "broad",
                "lines": _collect_broad_ports(ss_text, max_lines),
            }

        env_variables = _capped(mp.get("env_variables", []), "env_variables")
        if env_variables:
            plan_results["env_variables"] = {
                "type": "plan",
                "items": _collect_env_variables(env_variables),
            }

        health_commands = _capped(mp.get("health_commands", []), "health_commands")
        if health_commands:
            plan_results["health_commands"] = {
                "type": "plan",
                "items": _collect_health_commands(health_commands),
            }

    context["plan_results"] = plan_results
    return context
