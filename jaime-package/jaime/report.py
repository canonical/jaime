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

logger = logging.getLogger(__name__)

_DEFAULT_REPORT_DIR = "/var/log/jaime/reports"
_MAX_CHARM_OPTIONS = 100


def _append(lines: list[str], *chunks: list[str]) -> None:
    for chunk in chunks:
        lines.extend(chunk)
        lines.append("")


def generate_report(
    incident_id: str,
    unit_name: str,
    workload: str,
    first_seen: str,
    context: dict,
    report_dir: str = "",
    status_message: str = "",
) -> str:
    """Generate a Markdown context report and write it to disk.

    Falls back to _DEFAULT_REPORT_DIR if report_dir is empty.
    Returns the path of the written report file.
    """
    report_dir = report_dir or _DEFAULT_REPORT_DIR
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    lines = []

    header = [
        "# Incident Report",
        f"- incident: {incident_id}",
        f"- unit: {unit_name}",
        f"- status: {workload}",
    ]
    if status_message:
        header.append(f"- status-message: {status_message}")
    header.extend([
        f"- first-seen: {first_seen}",
        f"- generated: {now}",
    ])
    _append(lines, header)

    plan_results = context.get("plan_results", {})

    _append_section_summary(lines, workload, context, plan_results, status_message)
    _append_section_network(lines, plan_results)
    _append_section_ss_connections(lines, context)
    _append_section_firewall_rules(lines, context)
    _append_section_log_files(lines, plan_results)
    _append_section_processes(lines, plan_results)
    _append_section_systemd(lines, plan_results, context)
    _append_section_env(lines, plan_results)
    _append_section_health_commands(lines, plan_results)

    # Background sections
    _append_section_snap(lines, context)
    _append_section_k8s_pod(lines, context)
    _append_section_k8s_previous_logs(lines, context)
    _append_section_k8s_events(lines, context)
    _append_section_k8s_resource_usage(lines, context)
    _append_section_juju_config(lines, context)
    _append_section_charm_config(lines, context)
    _append_section_charm_actions(lines, context)
    _append_section_charm_metadata(lines, context)
    _append_section_disk(lines, context)
    _append_section_memory(lines, context)
    _append_section_logs(lines, context)

    content = "\n".join(lines)

    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{incident_id}.md")
    with open(report_path, "w") as f:
        f.write(content)

    logger.debug("report written to %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Per-section append helpers
# ---------------------------------------------------------------------------


def _append_section_summary(lines: list[str], workload: str,
                             context: dict, plan_results: dict,
                             status_message: str = "") -> None:
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
    error_lines = [
        line for line in unit_logs
        if "ERROR" in line.upper() or "WARNING" in line.upper()
    ]
    error_lines = deduplicate_lines(error_lines)[-10:]
    if error_lines:
        summary.append("")
        summary.append("**Recent errors/warnings:**")
        summary.append("```")
        summary.extend(error_lines)
        summary.append("```")

    # Operator-changed Juju config options (k8s: from Application.Get).
    # These show operator intent and are often directly tied to the incident.
    juju_config = context.get("juju_config", {})
    user_changed = {
        k: v for k, v in juju_config.items() if v.get("source") == "user"
    }
    if user_changed:
        summary.append("")
        summary.append("**Config changed from default by operator:**")
        for k, v in sorted(user_changed.items()):
            summary.append(
                f"- `{k}`: `{v.get('value')}` (default: `{v.get('default')}`)"
            )

    # Charm config options that are explicitly set (non-empty, non-False).
    charm_config = context.get("charm_config", {})
    config_yaml = charm_config.get("config_yaml", "")
    if config_yaml:
        try:
            parsed = yaml.safe_load(config_yaml)
            options = (parsed or {}).get("options", {})
            set_options = {
                k: v.get("default")
                for k, v in options.items()
                if v.get("default") not in (None, "", False, "False", "false")
            }
            if set_options:
                summary.append("")
                summary.append("**Charm options with non-empty schema defaults:**")
                for k, v in sorted(set_options.items()):
                    summary.append(f"- `{k}`: `{v}`")
        except Exception:
            pass

    _append(lines, summary)


def _append_section_log_files(lines: list[str], plan_results: dict) -> None:
    section = plan_results.get("log_files")
    if not section:
        return

    if section["type"] == "plan":
        _append(lines, ["## Log files"])
        for item in section.get("items", []):
            path = item.get("path", "")
            priority = item.get("priority", "")
            status = item.get("status", "")
            desc = item.get("description", "")
            tag = f" ({priority})" if priority else ""
            label = f"{path}{tag} — _{desc}_" if desc else f"{path}{tag}"
            if status == "available":
                _append(lines, [f"- {label} ✓"])
                item_lines = item.get("lines", [])
                if item_lines:
                    _append(lines, ["```", *item_lines, "```"])
            else:
                _append(lines, [f"- {label} ✗ ({status})"])


def _append_section_processes(lines: list[str], plan_results: dict) -> None:
    section = plan_results.get("processes")
    if not section:
        return

    if section["type"] == "plan":
        _append(lines, ["## Processes"])
        for item in section.get("items", []):
            name = item.get("name", "")
            count = item.get("running_count", 0)
            expected_min = item.get("expected_min_count", 1)
            expected_max = item.get("expected_max_count", 1)
            status = item.get("status", "")
            summary = f"{count} running (expected {expected_min}-{expected_max})"
            icon = "✓" if status == "ok" else "✗"
            _append(lines, [f"- **{name}**: {summary} {icon}"])
    else:
        raw_lines = section.get("lines", [])
        if raw_lines:
            _append(lines, ["## Processes", "```", *raw_lines, "```"])


def _systemd_line(item: dict) -> str:
    """One compact line for a systemd unit: state plus restart/exit detail.

    `NRestarts` and `ExecMainStatus` are what diagnose a service that is
    looping or exiting non-zero, so they are worth the few extra characters.
    """
    unit = item.get("unit", "")
    status = item.get("status", "")
    icon = "✓" if status == "active" else "✗"
    detail = []
    if item.get("substate"):
        detail.append(str(item["substate"]))
    if item.get("restarts") not in (None, "", "0"):
        detail.append(f"restarts={item['restarts']}")
    if item.get("exec_main_status") not in (None, "", "0"):
        detail.append(f"exec={item['exec_main_status']}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    return f"- `{unit}` → {status}{suffix} {icon}"


def _append_section_systemd(lines: list[str], plan_results: dict, context: dict) -> None:
    section = plan_results.get("systemd_units")

    if section and section["type"] == "plan":
        _append(lines, ["## Systemd units"])
        for item in section.get("items", []):
            _append(lines, [_systemd_line(item)])
    else:
        systemd_failed = context.get("systemd_failed", [])
        detail = context.get("systemd_failed_detail", [])
        if not section and not systemd_failed:
            return
        _append(lines, ["## Failed systemd units"])
        if detail:
            for item in detail:
                _append(lines, [_systemd_line(item)])
        elif systemd_failed:
            for unit in systemd_failed:
                _append(lines, [f"- `{unit}`"])
        elif section and section["type"] == "broad":
            broad_lines = section.get("lines", [])
            if broad_lines:
                for unit_line in broad_lines:
                    _append(lines, [f"- `{unit_line}`"])
            else:
                _append(lines, ["_None detected._"])
        else:
            _append(lines, ["_None detected._"])


def _append_section_network(lines: list[str], plan_results: dict) -> None:
    section = plan_results.get("network_ports")
    if not section:
        return

    if section["type"] == "plan":
        _append(lines, ["## Network ports"])
        for item in section.get("items", []):
            port = item.get("port", "")
            protocol = item.get("protocol", "tcp")
            status = item.get("status", "")
            icon = "✓" if status == "listening" else "✗"
            _append(lines, [f"- `{port}/{protocol}` → {status} {icon}"])
    else:
        raw_lines = section.get("lines", [])
        if raw_lines:
            _append(lines, ["## Network ports", "```", *raw_lines, "```"])


def _append_section_env(lines: list[str], plan_results: dict) -> None:
    section = plan_results.get("env_variables")
    if not section or section["type"] != "plan":
        return

    _append(lines, ["## Environment variables"])
    _append(lines, ["_Only whether each variable is set; values are never collected._"])
    for item in section.get("items", []):
        name = item.get("name", "")
        status = item.get("status", "")
        if status == "set":
            _append(lines, [f"- `{name}` — set ✓"])
        else:
            _append(lines, [f"- `{name}` — unset ✗"])


def _append_section_health_commands(lines: list[str], plan_results: dict) -> None:
    section = plan_results.get("health_commands")
    if not section or section["type"] != "plan":
        return

    _append(lines, ["## Health commands"])
    for item in section.get("items", []):
        command = item.get("command", "")
        returncode = item.get("returncode", 0)
        stdout = item.get("stdout", "")
        stderr = item.get("stderr", "")
        icon = "✓" if returncode == 0 else "✗"
        _append(lines, [f"- `$ {command}` → exit {returncode} {icon}"])
        if stdout:
            _append(lines, ["  ```", *stdout.splitlines(), "  ```"])
        if stderr:
            _append(lines, ["  ```", *stderr.splitlines(), "  ```"])


def _append_section_snap(lines: list[str], context: dict) -> None:
    """Snap status and failed-service logs.

    The reader (context["snap"]) is empty on hosts without snapd and when no
    service is in the failed state, so the whole section is omitted then.
    """
    snap = context.get("snap") or {}
    if not snap:
        return

    _append(lines, ["## Snap packages"])
    packages = snap.get("packages", [])
    if packages:
        _append(lines, ["```", *packages, "```"])
    else:
        _append(lines, ["_Not available._"])

    services = snap.get("services", [])
    if services:
        _append(lines, ["## Snap services", "```", *services, "```"])

    failed_services = snap.get("failed_services", [])
    if failed_services:
        _append(lines, ["**Failed services:** " + ", ".join(f"`{s}`" for s in failed_services)])

    failed_changes = snap.get("failed_changes", [])
    if failed_changes:
        _append(lines, ["## Failed snap changes", "```", *failed_changes, "```"])

    for service, snap_lines in sorted((snap.get("logs") or {}).items()):
        _append(lines, [
            f"## Snap logs: `{service}` (failed)", "```", *snap_lines, "```",
        ])


def _append_section_k8s_previous_logs(lines: list[str], context: dict) -> None:
    previous = context.get("k8s_previous_logs", [])
    if not previous:
        return
    _append(lines, ["## Previous container logs", "```", *previous, "```"])


def _append_section_charm_config(lines: list[str], context: dict) -> None:
    charm_config = context.get("charm_config", {})
    config_yaml = charm_config.get("config_yaml", "")
    if not config_yaml:
        return

    try:
        parsed = yaml.safe_load(config_yaml)
        options = (parsed or {}).get("options", {})
    except Exception as e:
        logger.debug("could not parse charm config YAML: %s", e)
        options = {}

    if not options:
        return

    _append(lines, ["## Charm config"])
    items = sorted(options.items())
    # Empty-string defaults are schema noise: the option is declared but has
    # no default value, so it carries no signal. Boolean/int False/0 defaults
    # are kept because "disabled" or "off" is meaningful.
    shown = [
        (key, opt) for key, opt in items
        if opt.get("default") not in (None, "")
    ]
    skipped_empty = len(items) - len(shown)
    for key, opt in shown[:_MAX_CHARM_OPTIONS]:
        default = opt.get("default", "")
        _append(lines, [f"- `{key}`: `{default}`"])
    omitted = []
    if skipped_empty:
        omitted.append(f"{skipped_empty} options with empty defaults")
    if len(shown) > _MAX_CHARM_OPTIONS:
        omitted.append(f"{len(shown) - _MAX_CHARM_OPTIONS} more options")
    if omitted:
        _append(lines, [f"_… {' and '.join(omitted)} omitted._"])
    if not shown:
        _append(lines, ["_No non-empty option defaults._"])


_ACTION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _append_section_charm_actions(lines: list[str], context: dict) -> None:
    """List the principal charm's declared actions (from its actions.yaml).

    The model is asked to suggest a remediation command, so it must know which
    ``juju run <unit> <action>`` commands actually exist. The machine collector
    already reads actions.yaml and carries it as ``charm_config.actions_yaml``;
    this section renders the action names (and their descriptions) so the
    suggestion cannot invent actions that are not there.
    """
    charm_config = context.get("charm_config", {})
    actions_yaml = charm_config.get("actions_yaml", "")
    if not actions_yaml:
        return

    try:
        parsed = yaml.safe_load(actions_yaml)
    except Exception as e:
        logger.debug("could not parse charm actions YAML: %s", e)
        return
    actions = parsed or {}
    if not isinstance(actions, dict) or not actions:
        return

    # Only the top-level action names are declarations; a bare YAML document
    # (e.g. empty or a scalar) is not a set of actions.
    declared = {
        name: (definition or {})
        for name, definition in actions.items()
        if _ACTION_NAME_RE.match(str(name)) and isinstance(definition, dict)
    }
    if not declared:
        return

    _append(lines, ["## Available actions"])
    for name in sorted(declared):
        description = declared[name].get("description", "")
        if description:
            _append(lines, [f"- `{name}` — {description}"])
        else:
            _append(lines, [f"- `{name}`"])


# Keys in a charm's metadata.yaml that are URLs (or lists of URLs) pointing at
# project resources. Rendered so the model can reference the official
# documentation, source, and issue tracker instead of guessing.
_METADATA_LINK_KEYS = (
    "docs", "website", "contact", "source", "issues", "bugs", "repository",
)


def _append_section_charm_metadata(lines: list[str], context: dict) -> None:
    """List the principal charm's project links from its metadata.yaml.

    The suggest prompt is asked to produce a remediation for a charm it does
    not know. The machine collector reads metadata.yaml; this section renders
    the documentation/source/issue URLs it declares so the model can reference
    the official docs rather than invent a procedure. k8s has no equivalent:
    a pod cannot read sibling applications' charm directories.
    """
    charm_config = context.get("charm_config", {})
    metadata_yaml = charm_config.get("metadata_yaml", "")
    if not metadata_yaml:
        return

    try:
        parsed = yaml.safe_load(metadata_yaml)
    except Exception as e:
        logger.debug("could not parse charm metadata YAML: %s", e)
        return
    meta = parsed or {}
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
    _append(lines, [
        "_Showing only lines matching `error` or `warning` (case-insensitive), "
        "with a context window around the last match._"
    ])
    _append(lines, ["_Logs are in chronological order._"])
    if unit_logs:
        _append(lines, ["```", *unit_logs, "```"])
    else:
        _append(lines, ["_No recent logs found._"])


def _append_section_ss_connections(lines: list[str], context: dict) -> None:
    ss = context.get("ss_connections", [])
    if not ss:
        return
    _append(lines, ["## Network connections (listening + active)", "```", *ss, "```"])


def _append_section_firewall_rules(lines: list[str], context: dict) -> None:
    fw = context.get("firewall_rules", {})
    if not fw:
        return

    iptables = fw.get("iptables", [])
    if iptables:
        _append(lines, ["## Firewall rules (iptables — IPv4)", "```", *iptables, "```"])

    ufw = fw.get("ufw", [])
    if ufw:
        _append(lines, ["## Firewall rules (ufw)", "```", *ufw, "```"])

    nftables = fw.get("nftables", [])
    if nftables:
        _append(lines, ["## Firewall rules (nftables — IPv4)", "```", *nftables, "```"])


def _append_section_juju_config(lines: list[str], context: dict) -> None:
    """Render the application's Juju config (from Application.Get).

    Each option maps to {default, description, source, type, value}.
    Options the operator explicitly changed (source=user) are called out
    first — they are the most diagnostic-relevant.
    """
    options = context.get("juju_config", {})
    if not options:
        return

    _append(lines, ["## Juju application config"])

    changed = {k: v for k, v in options.items() if v.get("source") == "user"}
    if changed:
        _append(lines, ["**Changed from default:**"])
        for key, opt in sorted(changed.items()):
            _append(lines, [
                f"- `{key}`: `{opt.get('value')}` (default: `{opt.get('default')}`)"
            ])

    _append(lines, ["**All options:**", "```"])
    for key, opt in sorted(options.items()):
        marker = " *" if opt.get("source") == "user" else ""
        _append(lines, [f"{key}: {opt.get('value')}{marker}"])
    _append(lines, ["```", "_(* = changed from default)_"])


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

    _append(lines, ["## Kubernetes pod"])
    _append(lines, [
        f"- name: `{pod.get('name', '')}`",
        f"- phase: `{pod.get('phase', 'unknown')}`",
    ])
    if pod.get("node"):
        _append(lines, [f"- node: `{pod['node']}`"])
    if pod.get("pod_ip"):
        _append(lines, [f"- pod IP: `{pod['pod_ip']}`"])
    if pod.get("qos"):
        _append(lines, [f"- QoS class: `{pod['qos']}`"])
    conditions = pod.get("conditions", [])
    if conditions:
        _append(lines, [f"- conditions: {', '.join(conditions)}"])

    containers = pod.get("containers", [])
    if containers:
        _append(lines, ["", "**Containers:**", ""])
        for c in containers:
            icon = "✓" if c.get("ready") else "✗"
            _append(lines, [
                f"- `{c.get('name')}` ({_container_label(c)}) "
                f"ready={c.get('ready')} restarts={c.get('restartCount')} {icon}",
                f"  image: `{c.get('image', '')}`",
            ])
            if c.get("resources"):
                _append(lines, [f"  resources: {c['resources']}"])
            for probe_kind in ("liveness", "readiness"):
                if c.get(probe_kind):
                    _append(lines, [f"  {probe_kind}: {c[probe_kind]}"])

    init_containers = pod.get("init_containers", [])
    if init_containers:
        _append(lines, ["", "**Init containers:**", ""])
        for c in init_containers:
            icon = "✓" if c.get("ready") else "✗"
            _append(lines, [
                f"- `{c.get('name')}` ({_container_label(c)}) ready={c.get('ready')} {icon}"
            ])

    volumes = pod.get("volumes", [])
    if volumes:
        _append(lines, ["", "**Volumes:**", ""])
        for v in volumes:
            mounts = f" → {', '.join(v['mounts'])}" if v.get("mounts") else ""
            _append(lines, [f"- `{v['name']}`: {v['source']}{mounts}"])


def _append_section_k8s_events(lines: list[str], context: dict) -> None:
    events = context.get("k8s_events", [])
    if not events:
        return
    _append(lines, ["## Kubernetes events", "```", *events, "```"])


def _append_section_k8s_resource_usage(lines: list[str], context: dict) -> None:
    usage = context.get("k8s_resource_usage", [])
    if not usage:
        return
    _append(lines, ["## Resource usage (metrics-server)", "```", *usage, "```"])
