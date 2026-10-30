"""Markdown report generation for Jaime incidents.

A report captures the machine context at the time of the incident:
logs, systemd state, disk, memory. It is the input provided to the LLM
in suggest/act mode. It does not contain LLM output.
"""

import datetime
import logging
import os
import re

import yaml

from jaime.logutils import deduplicate_lines
from jaime.redact import (
    is_secret_option,
    redact_config_value,
    redact_text,
)

logger = logging.getLogger(__name__)

_DEFAULT_REPORT_DIR = "/var/log/jaime/reports"
_MAX_CHARM_OPTIONS = 100


def _append(lines: list[str], *chunks: list[str]) -> None:
    for chunk in chunks:
        lines.extend(chunk)
        lines.append("")


def _append_fenced(lines: list[str], title: str, body: list[str]) -> None:
    """Append ``title`` and ``body`` in a code fence; nothing when ``body`` is empty."""
    if body:
        _append(lines, [title, "```", *body, "```"])


def _icon(ok: bool) -> str:
    return "✓" if ok else "✗"


def _plan_results(context: dict) -> dict:
    return context.get("plan_results", {})


def _plan_items(context: dict, key: str) -> list[dict] | None:
    """Items of a plan-driven section, or None when it is absent or not a plan."""
    section = _plan_results(context).get(key)
    if section and section["type"] == "plan":
        return section.get("items", [])
    return None


def _parse_yaml(text: str, what: str):
    """Parse a charm YAML file; None (logged at debug level) when it is invalid."""
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as e:
        logger.debug("could not parse charm %s YAML: %s", what, e)
        return None


def _charm_config_options(context: dict) -> dict | None:
    """The principal charm's declared options (``config.yaml``), name to schema.

    None when no ``config.yaml`` was collected, and an empty dict when it is
    present but unparsable or has no usable ``options`` mapping.
    """
    config_yaml = (context.get("charm_config") or {}).get("config_yaml", "")
    if not config_yaml:
        return None
    parsed = _parse_yaml(config_yaml, "config")
    options = parsed.get("options") if isinstance(parsed, dict) else None
    if not isinstance(options, dict):
        return {}
    return {name: opt for name, opt in options.items() if isinstance(opt, dict)}


_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def compact_log_lines(lines: list[str]) -> list[str]:
    """Compact timestamped log lines into a timeline with full-date stamps.

    Consecutive identical messages (ignoring the leading timestamp) are
    collapsed to their first occurrence with a ``×N`` repeat count, and every
    entry keeps a full ``YYYY-MM-DD HH:MM:SS`` timestamp. Lines without a
    leading timestamp (e.g. dedup markers) pass through unchanged. Used for the
    report-level rendering of unit and journal logs.
    """
    result: list[str] = []
    last_message: str | None = None
    last_ts = ""
    run = 0

    def flush() -> None:
        nonlocal last_message, last_ts, run
        if last_message is None:
            return
        count = f" ×{run}" if run > 1 else ""
        result.append(f"{last_ts}  {last_message}{count}")

    for line in lines:
        m = _LOG_TIMESTAMP_RE.match(line)
        if m:
            ts = f"{m.group(1)} {m.group(2)}"
            message = line[m.end() :].strip()
            if message == last_message:
                run += 1
                continue
            flush()
            last_message, last_ts, run = message, ts, 1
        else:
            flush()
            last_message, last_ts, run = None, "", 0
            result.append(line.rstrip())
    flush()
    return result


_SS_LINE_RE = re.compile(
    r"^(?P<netid>\S+)\s+(?P<state>\S+)\s+\S+\s+\S+\s+"
    r"(?P<local>\S+)\s+(?P<peer>\S+)(?:\s+(?P<proc>.*))?$"
)


def digest_network_ports(lines: list[str]) -> list[str]:
    """Compact an ``ss`` dump to ``local-address:port  owner``.

    Drops the header row and the netid/state/queue/peer columns; the owner is
    the program name from ``users:(("name",pid=...,fd=...))`` when present.
    Lines that do not parse (e.g. already-digested input) pass through.
    """
    result: list[str] = []
    for line in lines:
        if line.lstrip().startswith("Netid"):
            continue
        m = _SS_LINE_RE.match(line.strip())
        if not m:
            result.append(line.rstrip())
            continue
        proc = (m.group("proc") or "").strip()
        owner = ""
        if proc:
            mm = re.search(r'users:\(\(\s*"([^"]+)"', proc)
            owner = mm.group(1) if mm else proc
        if owner:
            result.append(f"- {m.group('local')}  {owner}")
        else:
            result.append(f"- {m.group('local')}")
    return result


def _config_options(context: dict) -> tuple[list[dict], bool] | None:
    """The monitored charm's config options with their current state.

    Merges the declared options (``config.yaml``, machine charm) with the live
    values from the controller (``juju_config``, whenever controller
    credentials are available). Each option is a dict with ``name``, ``value``,
    ``default``, ``type`` and ``changed`` (true when the live value differs
    from the default). Returns ``(options, live)`` where ``live`` says whether
    current values are known; without them ``value`` is the declared default
    and nothing is marked changed. None when there is no config data at all.
    """
    declared = _charm_config_options(context)
    live_options = context.get("juju_config") or {}
    if declared is None and not live_options:
        return None

    options = []
    for name in sorted(set(declared or {}) | set(live_options)):
        schema = (declared or {}).get(name, {})
        live = live_options.get(name)
        if live is None:
            default = schema.get("default")
            options.append(
                {"name": name, "value": default, "default": default, "type": schema.get("type"), "changed": False}
            )
            continue
        value = live.get("value", live.get("default"))
        default = live.get("default", schema.get("default"))
        # An option is changed when the operator set it to something other than
        # the default; setting it to the default value again changes nothing.
        changed = live.get("source") == "user" and (default is None or str(value) != str(default))
        options.append(
            {
                "name": name,
                "value": value,
                "default": default,
                "type": live.get("type") or schema.get("type"),
                "changed": changed,
            }
        )
    return options, bool(live_options)


def _shown(option: dict, key: str) -> str:
    """Redacted rendering of an option's ``value`` or ``default``; empty shows as ``""``."""
    return redact_config_value(option["name"], option[key], option["type"]) or '""'


def _config_line(option: dict) -> str:
    """One changed option as a Markdown list item.

    A sensitive option keeps its name and drops the value and default, so the
    report stays diagnostic without exposing either.
    """
    value = _shown(option, "value")
    if is_secret_option(option["name"], option["value"], option["type"]):
        return f"- `{option['name']}`: `{value}`"
    return f"- `{option['name']}`: `{value}` (default: `{_shown(option, 'default')}`)"


def _report_header(
    incident_id: str,
    unit_name: str,
    workload: str,
    first_seen: str,
    context: dict,
    status_message: str,
) -> list[str]:
    """The ``# Incident Report`` title and its key/value block."""
    header = [
        "# Incident Report",
        f"- incident: {incident_id}",
        f"- unit: {unit_name}",
    ]
    # Charm identity of the monitored application: channel (track/risk),
    # revision, origin, base and version come from the controller's FullStatus.
    # Omitted entirely when unavailable (machine charm without controller creds).
    app_meta = context.get("app_metadata") or {}
    for key, label in (
        ("name", "charm"),
        ("channel", "channel"),
        ("revision", "revision"),
        ("origin", "origin"),
        ("base", "base"),
        ("version", "version"),
    ):
        value = app_meta.get(key)
        if value is None or value == "":
            continue
        header.append(f"- {label}: {value}")
    header.append(f"- status: {workload}")
    if status_message:
        header.append(f"- status-message: {status_message}")
    header.extend(
        [
            f"- first-seen: {first_seen}",
            f"- generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        ]
    )
    return header


def render_report(
    incident_id: str,
    unit_name: str,
    workload: str,
    first_seen: str,
    context: dict,
    status_message: str = "",
) -> str:
    """Render the Markdown context report, with secrets redacted."""
    lines: list[str] = []
    _append(lines, _report_header(incident_id, unit_name, workload, first_seen, context, status_message))
    _append_section_summary(lines, workload, context, status_message)
    for append_section in _SECTIONS:
        append_section(lines, context)
    return redact_text("\n".join(lines))


def report_path(report_dir: str, file_name: str) -> str:
    """Path of a report file, falling back to the default report directory."""
    return os.path.join(report_dir or _DEFAULT_REPORT_DIR, file_name)


def _version_file_name(report_dir: str, incident_id: str) -> str:
    """A fresh ``<incident-id>-<UTC timestamp>.md`` name that is not taken yet."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{incident_id}-{stamp}.md"
    suffix = 1
    while os.path.exists(os.path.join(report_dir, name)):
        name = f"{incident_id}-{stamp}-{suffix}.md"
        suffix += 1
    return name


def generate_report(
    incident_id: str,
    unit_name: str,
    workload: str,
    first_seen: str,
    context: dict,
    report_dir: str = "",
    status_message: str = "",
    versioned: bool = False,
) -> str:
    """Generate a Markdown context report and write it to disk.

    Falls back to _DEFAULT_REPORT_DIR if report_dir is empty. The first report
    of an incident is ``<incident-id>.md``; with ``versioned`` a new
    ``<incident-id>-<UTC timestamp>.md`` is written instead, so an earlier
    version, and the AI answers based on it, is never overwritten.
    Returns the path of the written report file.
    """
    content = render_report(incident_id, unit_name, workload, first_seen, context, status_message)

    report_dir = report_dir or _DEFAULT_REPORT_DIR
    os.makedirs(report_dir, exist_ok=True)
    name = _version_file_name(report_dir, incident_id) if versioned else f"{incident_id}.md"
    report_path = os.path.join(report_dir, name)
    with open(report_path, "w") as f:
        f.write(content)

    logger.debug("report written to %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Per-section append helpers
# ---------------------------------------------------------------------------


def _append_section_summary(lines: list[str], workload: str, context: dict, status_message: str = "") -> None:
    """Compact executive summary — highest-signal content first.

    Surfaces the workload status message (what Juju reports as the reason the
    unit is unhealthy), then error/warning log lines and explicitly-set config
    options so the LLM can form a diagnosis before reading the full detail
    sections.
    """
    summary = ["## Executive summary", f"Unit is in **{workload}** state."]
    if status_message:
        summary.append(f"Status message: {status_message}")

    # Most recent error/warning log lines (up to 10), deduplicated so a burst
    # of identical failures (e.g. health-check errors firing every few seconds)
    # cannot push the causal error out of the summary.
    unit_logs = context.get("unit_logs", [])
    error_lines = [line for line in unit_logs if "ERROR" in line.upper() or "WARNING" in line.upper()]
    error_lines = deduplicate_lines(error_lines)[-10:]
    if error_lines:
        summary.append("")
        summary.append("**Recent errors/warnings:**")
        summary.append("```")
        summary.extend(error_lines)
        summary.append("```")

    # Config options the operator changed from their default. These show
    # operator intent and are often directly tied to the incident.
    merged = _config_options(context)
    changed = [opt for opt in (merged[0] if merged else []) if opt["changed"]]
    if changed:
        summary.append("")
        summary.append("**Config changed from default by operator:**")
        summary.extend(_config_line(opt) for opt in changed)

    _append(lines, summary)


def _append_section_log_files(lines: list[str], context: dict) -> None:
    items = _plan_items(context, "log_files")
    if items is None:
        return

    block = ["## Log files"]
    for item in items:
        path = item.get("path", "")
        priority = item.get("priority", "")
        status = item.get("status", "")
        desc = item.get("description", "")
        tag = f" ({priority})" if priority else ""
        label = f"{path}{tag} — _{desc}_" if desc else f"{path}{tag}"
        if status == "available":
            block.append(f"- {label} ✓")
            item_lines = item.get("lines", [])
            if item_lines:
                block.extend(["```", *item_lines, "```"])
        else:
            block.append(f"- {label} ✗ ({status})")
    _append(lines, block)


def _append_section_processes(lines: list[str], context: dict) -> None:
    section = _plan_results(context).get("processes")
    if not section:
        return

    if section["type"] == "plan":
        block = ["## Processes"]
        for item in section.get("items", []):
            name = item.get("name", "")
            count = item.get("running_count", 0)
            expected_min = item.get("expected_min_count", 1)
            expected_max = item.get("expected_max_count", 1)
            summary = f"{count} running (expected {expected_min}-{expected_max})"
            block.append(f"- **{name}**: {summary} {_icon(item.get('status', '') == 'ok')}")
        _append(lines, block)
    else:
        _append_fenced(lines, "## Processes", section.get("lines", []))


def _systemd_line(item: dict) -> str:
    """One compact line for a systemd unit: state plus restart/exit detail.

    `NRestarts` and `ExecMainStatus` are what diagnose a service that is
    looping or exiting non-zero, so they are worth the few extra characters.
    """
    unit = item.get("unit", "")
    status = item.get("status", "")
    detail = []
    if item.get("substate"):
        detail.append(str(item["substate"]))
    if item.get("restarts") not in (None, "", "0"):
        detail.append(f"restarts={item['restarts']}")
    if item.get("exec_main_status") not in (None, "", "0"):
        detail.append(f"exec={item['exec_main_status']}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    return f"- `{unit}` → {status}{suffix} {_icon(status == 'active')}"


def _append_section_systemd(lines: list[str], context: dict) -> None:
    items = _plan_items(context, "systemd_units")
    if items is not None:
        _append(lines, ["## Systemd units", *[_systemd_line(item) for item in items]])
        return

    section = _plan_results(context).get("systemd_units")
    systemd_failed = context.get("systemd_failed", [])
    if not section and not systemd_failed:
        return

    detail = context.get("systemd_failed_detail", [])
    broad_lines = section.get("lines", []) if section and section["type"] == "broad" else []
    if detail:
        entries = [_systemd_line(item) for item in detail]
    elif systemd_failed:
        entries = [f"- `{unit}`" for unit in systemd_failed]
    elif broad_lines:
        entries = [f"- `{unit_line}`" for unit_line in broad_lines]
    else:
        entries = ["_None detected._"]
    _append(lines, ["## Failed systemd units", *entries])


def _append_section_network(lines: list[str], context: dict) -> None:
    section = _plan_results(context).get("network_ports")
    if not section:
        return

    if section["type"] == "plan":
        port_lines = []
        for item in section.get("items", []):
            port = item.get("port", "")
            protocol = item.get("protocol", "tcp")
            status = item.get("status", "")
            # The machine plan reports whether a port is listening; the k8s
            # plan can only report that it is declared in the pod spec. Both
            # are a pass, but the printed status keeps the distinction.
            port_lines.append(f"- `{port}/{protocol}` → {status} {_icon(status in ('listening', 'declared'))}")
        if port_lines:
            _append(lines, ["## Network ports", *port_lines])
    else:
        raw_lines = section.get("lines", [])
        if raw_lines:
            _append(lines, ["## Network ports", *digest_network_ports(raw_lines)])


def _append_section_plan_containers(lines: list[str], context: dict) -> None:
    items = _plan_items(context, "plan_containers")
    if items is None:
        return

    entries = [
        f"- `{item.get('name', '')}` → {'found ✓' if item.get('status', '') == 'found' else 'not found ✗'}"
        for item in items
    ]
    _append(lines, ["## Plan containers", *entries])


def _append_section_env(lines: list[str], context: dict) -> None:
    items = _plan_items(context, "env_variables")
    if items is None:
        return

    entries = [
        f"- `{item.get('name', '')}` — {'set ✓' if item.get('status', '') == 'set' else 'unset ✗'}" for item in items
    ]
    _append(
        lines,
        ["## Environment variables", "_Only whether each variable is set; values are never collected._", *entries],
    )


def _append_section_health_commands(lines: list[str], context: dict) -> None:
    items = _plan_items(context, "health_commands")
    if items is None:
        return

    block = ["## Health commands"]
    for item in items:
        command = item.get("command", "")
        returncode = item.get("returncode", 0)
        stdout = item.get("stdout", "")
        stderr = item.get("stderr", "")
        block.append(f"- `$ {command}` → exit {returncode} {_icon(returncode == 0)}")
        if stdout:
            block.extend(["  ```", *stdout.splitlines(), "  ```"])
        if stderr:
            block.extend(["  ```", *stderr.splitlines(), "  ```"])
    _append(lines, block)


def _append_section_snap(lines: list[str], context: dict) -> None:
    """Snap status and failed-service logs.

    The reader (context["snap"]) is empty on hosts without snapd and when no
    service is in the failed state, so the whole section is omitted then. The
    installed-snap inventory itself lives in the always-on `## Snap packages`
    section rendered from `context["snap_list"]`.
    """
    snap = context.get("snap") or {}
    if not snap:
        return

    _append_fenced(lines, "## Snap services", snap.get("services", []))

    failed_services = snap.get("failed_services", [])
    if failed_services:
        _append(lines, ["**Failed services:** " + ", ".join(f"`{s}`" for s in failed_services)])

    _append_fenced(lines, "## Failed snap changes", snap.get("failed_changes", []))

    for service, snap_lines in sorted((snap.get("logs") or {}).items()):
        _append(lines, [f"## Snap logs: `{service}` (failed)", "```", *snap_lines, "```"])


def _append_section_snap_list(lines: list[str], context: dict) -> None:
    """Always-on inventory of installed snaps, when snapd is present."""
    _append_fenced(lines, "## Snap packages", context.get("snap_list") or [])


def _append_section_journal(lines: list[str], context: dict) -> None:
    """Journalctl errors/warnings for the relevant units, time-bounded."""
    journal = context.get("journal") or []
    _append_fenced(lines, "## Journal (errors/warnings)", compact_log_lines(journal))


def _append_section_k8s_previous_logs(lines: list[str], context: dict) -> None:
    _append_fenced(lines, "## Previous container logs", context.get("k8s_previous_logs", []))


def _append_section_charm_config(lines: list[str], context: dict) -> None:
    """Every config option of the monitored charm, with its value.

    Options changed from their default are listed first and flagged, so they
    survive the cap. When current values are unavailable (machine charm without
    controller credentials) the declared defaults are shown instead, and the
    section says so.
    """
    merged = _config_options(context)
    if merged is None:
        return
    options, live = merged

    if not options:
        # Present but unparsable/structurally broken config - surface it
        # instead of silently dropping the section (an invisible truncation
        # was masking the whole section before the collector byte-cap fix).
        _append(lines, ["## Charm config", "_Could not parse config.yaml._"])
        return

    ordered = sorted(options, key=lambda opt: (not opt["changed"], opt["name"]))
    body = []
    for opt in ordered[:_MAX_CHARM_OPTIONS]:
        line = f"{opt['name']}: {_shown(opt, 'value')}"
        if opt["changed"]:
            if is_secret_option(opt["name"], opt["value"], opt["type"]):
                line += "  # changed from default"
            else:
                line += f"  # changed from default ({_shown(opt, 'default')})"
        body.append(line)

    section = ["## Charm config"]
    if not live:
        section.append("_Current values unavailable (no controller credentials): showing the declared defaults._")
    section.extend(["```", *body, "```"])
    if len(ordered) > _MAX_CHARM_OPTIONS:
        section.append(f"_… {len(ordered) - _MAX_CHARM_OPTIONS} more options omitted._")
    _append(lines, section)


_ACTION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _append_section_charm_actions(lines: list[str], context: dict) -> None:
    """List the principal charm's declared actions (from its actions.yaml).

    The model is asked to suggest a remediation command, so it must know which
    ``juju run <unit> <action>`` commands actually exist. The machine collector
    already reads actions.yaml and carries it as ``charm_config.actions_yaml``;
    this section renders the action names (and their descriptions) so the
    suggestion cannot invent actions that are not there.
    """
    actions_yaml = (context.get("charm_config") or {}).get("actions_yaml", "")
    if not actions_yaml:
        return

    actions = _parse_yaml(actions_yaml, "actions")
    if not isinstance(actions, dict):
        return

    # Only the top-level action names are declarations; a bare YAML document
    # (e.g. empty or a scalar) is not a set of actions.
    declared = {
        name: definition
        for name, definition in actions.items()
        if _ACTION_NAME_RE.match(str(name)) and isinstance(definition, dict)
    }
    if not declared:
        return

    # Rendered as a compact YAML block (name: description) - the actions'
    # native format is actions.yaml - with no blank lines between entries.
    # Multi-line descriptions are collapsed to a single line so the block has
    # no empty lines inside.
    actions_block = yaml.safe_dump(
        {name: " ".join((declared[name].get("description", "") or "").split()) for name in sorted(declared)},
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    _append(lines, ["## Available actions", "```yaml", *actions_block.splitlines(), "```"])


# Keys in a charm's metadata.yaml that are URLs (or lists of URLs) pointing at
# project resources. Rendered so the model can reference the official
# documentation, source, and issue tracker instead of guessing.
_METADATA_LINK_KEYS = (
    "docs",
    "website",
    "contact",
    "source",
    "issues",
    "bugs",
    "repository",
)


def _append_section_charm_metadata(lines: list[str], context: dict) -> None:
    """List the principal charm's project links from its metadata.yaml.

    The suggest prompt is asked to produce a remediation for a charm it does
    not know. The machine collector reads metadata.yaml; this section renders
    the documentation/source/issue URLs it declares so the model can reference
    the official docs rather than invent a procedure. k8s has no equivalent:
    a pod cannot read sibling applications' charm directories.
    """
    metadata_yaml = (context.get("charm_config") or {}).get("metadata_yaml", "")
    if not metadata_yaml:
        return

    meta = _parse_yaml(metadata_yaml, "metadata")
    if not isinstance(meta, dict):
        return

    sections: list[str] = []
    for key in _METADATA_LINK_KEYS:
        value = meta.get(key)
        if not value:
            continue
        if isinstance(value, str):
            sections.append(f"- {key}: {value}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.startswith("http"):
                    sections.append(f"- {key}: {item}")
    if not sections:
        return

    _append(lines, ["## Charm links"])
    _append(lines, sections)


def _append_section_disk(lines: list[str], context: dict) -> None:
    disk = context.get("disk_usage", [])
    _append(lines, ["## Disk usage"])
    if disk:
        # Filter out snap mount lines — they always show 100% and are not
        # relevant to workload disk health.
        filtered = [line for line in disk if "/snap/" not in line]
        _append(lines, ["```", *filtered, "```"])
    else:
        _append(lines, ["_Not available._"])


def _append_section_memory(lines: list[str], context: dict) -> None:
    memory = context.get("memory_summary", [])
    _append(lines, ["## Memory"])
    if not memory:
        _append(lines, ["_Not available._"])
        return

    # Parse `free -h` output into compact single lines per row.
    # Format: "Mem:  total  used  free  shared  buff/cache  available"
    summary_lines = []
    for line in memory:
        parts = line.split()
        if not parts:
            continue
        label = parts[0].rstrip(":")
        if label in ("Mem", "Swap") and len(parts) >= 3:
            total = parts[1]
            used = parts[2]
            available = parts[6] if label == "Mem" and len(parts) >= 7 else parts[3]
            if label == "Mem":
                summary_lines.append(f"RAM: {used} used / {total} total ({available} available)")
            else:
                summary_lines.append(f"Swap: {used} used / {total} total")

    if summary_lines:
        _append(lines, summary_lines)
    else:
        # Fallback to raw output if parsing failed.
        _append(lines, ["```", *memory, "```"])


def _append_section_logs(lines: list[str], context: dict) -> None:
    unit_logs = context.get("unit_logs", [])
    _append(lines, ["## Recent unit logs"])
    _append(
        lines,
        [
            "_Showing only lines matching `error` or `warning` (case-insensitive), "
            "with a context window around the last match._"
        ],
    )
    _append(lines, ["_Logs are in chronological order._"])
    if unit_logs:
        _append(lines, ["```", *compact_log_lines(unit_logs), "```"])
    else:
        _append(lines, ["_No recent logs found._"])


def _append_section_ss_connections(lines: list[str], context: dict) -> None:
    _append_fenced(lines, "## Network connections (listening + active)", context.get("ss_connections", []))


def _append_section_firewall_rules(lines: list[str], context: dict) -> None:
    fw = context.get("firewall_rules", {})
    if not fw:
        return

    _append_fenced(lines, "## Firewall rules (iptables — IPv4)", fw.get("iptables", []))
    _append_fenced(lines, "## Firewall rules (ufw)", fw.get("ufw", []))
    _append_fenced(lines, "## Firewall rules (nftables — IPv4)", fw.get("nftables", []))


def _container_label(c: dict) -> str:
    """Container state including the reason and exit code.

    The reason is the diagnosis: `CrashLoopBackOff` while waiting, `OOMKilled`
    with an exit code once terminated.
    """
    label = c.get("state", "?")
    if c.get("state_reason"):
        label += f"/{c['state_reason']}"
    if c.get("exit_code"):
        label += f" exit={c['exit_code']}"
    last = c.get("last_state")
    if last and last != "unknown":
        last_label = last
        if c.get("last_state_reason"):
            last_label += f"/{c['last_state_reason']}"
        if c.get("last_exit_code"):
            last_label += f" exit={c['last_exit_code']}"
        label += f", last={last_label}"
    return label


def _append_section_k8s_pod(lines: list[str], context: dict) -> None:
    pod = context.get("k8s_pod", {})
    if not pod:
        return

    block = [
        "## Kubernetes pod",
        f"- name: `{pod.get('name', '')}`",
        f"- phase: `{pod.get('phase', 'unknown')}`",
    ]
    if pod.get("node"):
        block.append(f"- node: `{pod['node']}`")
    if pod.get("pod_ip"):
        block.append(f"- pod IP: `{pod['pod_ip']}`")
    if pod.get("qos"):
        block.append(f"- QoS class: `{pod['qos']}`")
    conditions = pod.get("conditions", [])
    if conditions:
        block.append(f"- conditions: {', '.join(conditions)}")
    _append(lines, block)

    containers = pod.get("containers", [])
    if containers:
        block = ["**Containers:**"]
        for c in containers:
            block.append(
                f"- `{c.get('name')}` ({_container_label(c)}) "
                f"ready={c.get('ready')} restarts={c.get('restartCount')} {_icon(c.get('ready'))}"
            )
            block.append(f"  image: `{c.get('image', '')}`")
            if c.get("resources"):
                block.append(f"  resources: {c['resources']}")
            for probe_kind in ("liveness", "readiness"):
                if c.get(probe_kind):
                    block.append(f"  {probe_kind}: {c[probe_kind]}")
        _append(lines, block)

    init_containers = pod.get("init_containers", [])
    if init_containers:
        _append(
            lines,
            [
                "**Init containers:**",
                *[
                    f"- `{c.get('name')}` ({_container_label(c)}) ready={c.get('ready')} {_icon(c.get('ready'))}"
                    for c in init_containers
                ],
            ],
        )

    volumes = pod.get("volumes", [])
    if volumes:
        block = ["**Volumes:**"]
        for v in volumes:
            mounts = f" → {', '.join(v['mounts'])}" if v.get("mounts") else ""
            block.append(f"- `{v['name']}`: {v['source']}{mounts}")
        _append(lines, block)


def _append_section_k8s_events(lines: list[str], context: dict) -> None:
    _append_fenced(lines, "## Kubernetes events", context.get("k8s_events", []))


def _append_section_k8s_resource_usage(lines: list[str], context: dict) -> None:
    _append_fenced(lines, "## Resource usage (metrics-server)", context.get("k8s_resource_usage", []))


# Sections rendered after the header and the executive summary, in report
# order. Every section takes ``(lines, context)`` and appends nothing when its
# data is absent. Add a new section here (and to scripts/generate_examples.py).
_SECTIONS = (
    _append_section_plan_containers,
    _append_section_network,
    _append_section_ss_connections,
    _append_section_firewall_rules,
    _append_section_log_files,
    _append_section_processes,
    _append_section_systemd,
    _append_section_env,
    _append_section_health_commands,
    # Background sections: collected for every incident, not plan-driven.
    _append_section_snap,
    _append_section_snap_list,
    _append_section_journal,
    _append_section_k8s_pod,
    _append_section_k8s_previous_logs,
    _append_section_k8s_events,
    _append_section_k8s_resource_usage,
    _append_section_charm_config,
    _append_section_charm_actions,
    _append_section_charm_metadata,
    _append_section_disk,
    _append_section_memory,
    _append_section_logs,
)
