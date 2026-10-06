"""Kubernetes context collection for Jaime K8s incidents.

Collects bounded diagnostics from the Kubernetes API without modifying any
state. All collection is read-only and bounded by time and line count.

Implements the same collect_context() interface as the machine collector,
so charm.py and report.py remain substrate-agnostic.
"""

import datetime
import logging
import re

from jaime.k8s_api import K8sApiClient
from jaime.logutils import cap_lines, deduplicate_lines, filter_error_context

logger = logging.getLogger(__name__)

_DEFAULT_LOG_WINDOW_MINUTES = 30
_DEFAULT_MAX_LINES = 500
# Hard cap on lines fetched from the k8s API per container. tailLines always
# returns the END of the window, so we fetch wide and filter client-side to
# keep the causal error at the start of the window.
_FETCH_LINES_CAP = 10000
# Runaway guards, not relevance judgements; see ARCHITECTURE.md, "Context
# evidence and prompt projection". Relevance is decided at prompt time (4.10).
_CAP_EVENTS = 200
_CAP_PREVIOUS_CONTAINERS = 3


def _container_state(state: dict) -> tuple[str, str, str]:
    """Return (kind, reason, exit_code) for a container state dict.

    ``state`` is ``running``, ``waiting`` or ``terminated``; the reason carries
    the diagnosis (``CrashLoopBackOff`` while waiting, ``OOMKilled`` with an
    exit code once terminated).
    """
    state = state or {}
    kind = next(iter(state), "unknown")
    info = state.get(kind) or {}
    exit_code = info.get("exitCode")
    return kind, info.get("reason", ""), ("" if exit_code is None else str(exit_code))


def _fmt_resources(resources: dict) -> str:
    """Compact rendering of container resource requests/limits."""
    parts = []
    for key in ("requests", "limits"):
        values = resources.get(key, {})
        if values:
            parts.append(f"{key}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    return "; ".join(parts)


def _fmt_probe(probe: dict | None) -> str:
    """Compact rendering of a liveness/readiness probe."""
    if not probe:
        return ""
    delay = probe.get("initialDelaySeconds", 0)
    suffix = f" (delay {delay}s)" if delay else ""
    if "httpGet" in probe:
        h = probe["httpGet"]
        return f"httpGet {h.get('path', '/')} :{h.get('port', '?')}{suffix}"
    if "exec" in probe:
        cmd = " ".join(probe["exec"].get("command", []))
        return f"exec `{cmd}`{suffix}"
    if "tcpSocket" in probe:
        return f"tcp :{probe['tcpSocket'].get('port', '?')}{suffix}"
    return ""


_VOLUME_SOURCE_KEYS = [
    ("configMap", lambda v: f"configMap/{v.get('name', '?')}"),
    ("secret", lambda v: f"secret/{v.get('secretName', '?')}"),
    ("persistentVolumeClaim", lambda v: f"pvc/{v.get('claimName', '?')}"),
    ("emptyDir", lambda v: "emptyDir"),
    ("hostPath", lambda v: f"hostPath/{v.get('path', '?')}"),
    ("projected", lambda v: "projected"),
    ("downwardAPI", lambda v: "downwardAPI"),
    ("serviceAccountToken", lambda v: "serviceAccountToken"),
    ("ephemeral", lambda v: "ephemeral"),
    ("csi", lambda v: f"csi/{v.get('driver', '?')}"),
    ("nfs", lambda v: f"nfs/{v.get('server', '?')}"),
]


def _fmt_volumes(spec: dict) -> list[dict]:
    """List pod volumes with their source and where they are mounted."""
    mounts_by_name: dict[str, list[str]] = {}
    for container in spec.get("containers", []):
        for m in container.get("volumeMounts", []):
            mounts_by_name.setdefault(m.get("name", ""), []).append(
                m.get("mountPath", "")
            )
    volumes = []
    for v in spec.get("volumes", []):
        source = "unknown"
        for key, fmt in _VOLUME_SOURCE_KEYS:
            if key in v:
                source = fmt(v[key])
                break
        volumes.append({
            "name": v.get("name", ""),
            "source": source,
            "mounts": mounts_by_name.get(v.get("name", ""), []),
        })
    return volumes


def _compile_patterns(plan: dict) -> list:
    """Compile plan log patterns, skipping any that fail to compile.

    Validation rejects invalid regexes, so a failure here means the collector
    was called with an unvalidated plan; skipping keeps collection safe.
    """
    patterns = []
    for pattern in plan.get("log_patterns", []) or []:
        try:
            patterns.append(re.compile(pattern))
        except (re.error, TypeError):
            logger.debug("ignoring invalid log pattern: %r", pattern)
    return patterns


def _select_logs(raw_lines: list[str], max_lines: int, patterns: list) -> list[str]:
    """Error/warning context plus plan pattern matches, deduplicated and capped.

    Pattern matches are appended in raw order, but only lines not already
    selected, so the error-context filter and the plan patterns cannot produce
    the same line twice.
    """
    selected = filter_error_context(raw_lines, max_lines)
    if not patterns:
        return cap_lines(deduplicate_lines(selected), max_lines)

    seen = set(selected)
    extra = []
    for line in raw_lines:
        if line in seen:
            continue
        if any(p.search(line) for p in patterns):
            extra.append(line)
            seen.add(line)
    return cap_lines(deduplicate_lines(selected + extra), max_lines)


def _declared_env_names(containers: list[dict]) -> set[str]:
    """Env variable names declared by the selected containers (never values)."""
    names = set()
    for container in containers:
        for env in container.get("env", []) or []:
            name = env.get("name")
            if name:
                names.add(name)
    return names


def _declared_ports(containers: list[dict]) -> set[tuple]:
    """(port, protocol) pairs declared by the selected containers."""
    ports = set()
    for container in containers:
        for port in container.get("ports", []) or []:
            ports.add((port.get("containerPort"), port.get("protocol", "tcp")))
    return ports


def _plan_results(plan: dict, spec: dict, selected: list[dict]) -> dict:
    """Build the plan check results report.py renders.

    An empty plan produces an empty dict, so an unconfigured charm emits no
    extra report sections.
    """
    results = {}
    if not plan:
        return results

    names = plan.get("containers")
    if names:
        present = {c.get("name", "") for c in spec.get("containers", [])}
        results["plan_containers"] = {
            "type": "plan",
            "items": [
                {"name": name, "status": "found" if name in present else "not found"}
                for name in names
            ],
        }

    env_names = plan.get("env_variables")
    if env_names:
        declared = _declared_env_names(selected)
        results["env_variables"] = {
            "type": "plan",
            "items": [
                {"name": name, "status": "set" if name in declared else "unset"}
                for name in env_names
            ],
        }

    ports = plan.get("ports")
    if ports:
        declared_ports = _declared_ports(selected)
        results["network_ports"] = {
            "type": "plan",
            "items": [
                {
                    "port": port.get("port"),
                    "protocol": port.get("protocol", "tcp"),
                    "status": "declared"
                    if (port.get("port"), port.get("protocol", "tcp")) in declared_ports
                    else "not declared",
                }
                for port in ports
            ],
        }

    return results


def collect_context(
    unit_name: str,
    log_window_minutes: int = _DEFAULT_LOG_WINDOW_MINUTES,
    max_lines: int = _DEFAULT_MAX_LINES,
    from_time: datetime.datetime | None = None,
    diagnostics_plan: dict | None = None,
) -> dict:
    """Collect bounded diagnostic context for a Kubernetes unit.

    ``unit_name`` is a Juju unit name (e.g. ``postgresql-k8s/0``); the pod is
    resolved via the ``unit.juju.is/id`` annotation. ``diagnostics_plan`` is the
    plan for this one application (the charm slices the keyed config object
    before calling); an empty or ``None`` plan keeps the fixed collection.

    Returns a context dict compatible with report.py, plus k8s-specific keys
    (``k8s_pod``, ``k8s_events``, ``k8s_resource_usage``).
    """
    plan = diagnostics_plan or {}
    now = datetime.datetime.now(datetime.timezone.utc)
    client = K8sApiClient()

    pod = client.get_pod_for_unit(unit_name)
    if pod is None:
        logger.debug("no pod found for unit %s", unit_name)
        return {
            "collected_at": now.isoformat(),
            "unit_logs": [],
            "k8s_previous_logs": [],
            "k8s_events": [],
            "k8s_pod": {},
            "k8s_resource_usage": [],
            "plan_results": {},
        }

    metadata = pod.get("metadata", {})
    pod_name = metadata.get("name", "")
    spec = pod.get("spec", {})
    status = pod.get("status", {})

    # Container selection: a plan may name the containers that matter. Env and
    # port checks and the log loops below use this selection; the pod summary
    # still describes every container.
    all_containers = spec.get("containers", [])
    plan_containers = plan.get("containers")
    if plan_containers:
        selected = [c for c in all_containers if c.get("name") in plan_containers]
    else:
        selected = all_containers
    patterns = _compile_patterns(plan)

    # Logs from the selected containers. Fetch a wide window (tailLines always
    # returns the END of the window), then keep error/warning lines with
    # context plus any plan pattern matches, so the causal error at the start
    # of the window is never pushed out by later noise. Finally deduplicate so
    # repeated failures (health checks firing every few seconds) collapse.
    unit_logs = []
    for container in selected:
        logs = client.get_pod_logs(
            pod_name,
            container=container.get("name"),
            since_time=from_time,
            tail_lines=_FETCH_LINES_CAP,
        )
        if logs:
            logs = _select_logs(logs, max_lines, patterns)
            unit_logs.append(f"=== container: {container.get('name')} ===")
            unit_logs.extend(logs)
    unit_logs = cap_lines(unit_logs, max_lines)

    # Previous-instance logs explain a crash loop; only for containers that
    # have actually restarted, only the selected ones, and only the first few.
    previous_logs = []
    started = 0
    for cs in status.get("containerStatuses", []):
        if started >= _CAP_PREVIOUS_CONTAINERS:
            break
        if cs.get("restartCount", 0) <= 0:
            continue
        if plan_containers and cs.get("name") not in plan_containers:
            continue
        logs = client.get_pod_logs(
            pod_name, container=cs.get("name"), previous=True,
            tail_lines=_FETCH_LINES_CAP,
        )
        if logs:
            started += 1
            logs = _select_logs(logs, max_lines, patterns)
            previous_logs.append(f"=== container (previous): {cs.get('name')} ===")
            previous_logs.extend(logs)
    previous_logs = cap_lines(previous_logs, max_lines)

    # Pod summary for the report: status (phase, conditions, readiness) merged
    # with spec details (resources, probes, volumes) from the pod definition.
    spec_by_name = {c.get("name", ""): c for c in spec.get("containers", [])}

    def _container_detail(cs: dict) -> dict:
        name = cs.get("name", "")
        cspec = spec_by_name.get(name, {})
        state_kind, state_reason, state_exit = _container_state(cs.get("state", {}))
        last_kind, last_reason, last_exit = _container_state(cs.get("lastState", {}))
        return {
            "name": name,
            "image": cs.get("image", ""),
            "ready": cs.get("ready", False),
            "restartCount": cs.get("restartCount", 0),
            "state": state_kind,
            "state_reason": state_reason,
            "exit_code": state_exit,
            "last_state": last_kind,
            "last_state_reason": last_reason,
            "last_exit_code": last_exit,
            "resources": _fmt_resources(cspec.get("resources", {})),
            "liveness": _fmt_probe(cspec.get("livenessProbe")),
            "readiness": _fmt_probe(cspec.get("readinessProbe")),
        }

    k8s_pod = {
        "name": pod_name,
        "phase": status.get("phase", "unknown"),
        "node": spec.get("nodeName", ""),
        "pod_ip": status.get("podIP", ""),
        "qos": status.get("qosClass", ""),
        "conditions": [
            f"{c.get('type')}={c.get('status')}"
            for c in status.get("conditions", [])
        ],
        "containers": [
            _container_detail(cs)
            for cs in status.get("containerStatuses", [])
        ],
        "init_containers": [
            _container_detail(cs)
            for cs in status.get("initContainerStatuses", [])
        ],
        "volumes": _fmt_volumes(spec),
    }

    return {
        "collected_at": now.isoformat(),
        "unit_logs": unit_logs,
        "k8s_previous_logs": previous_logs,
        "k8s_events": cap_lines(
            client.get_pod_events(pod_name, limit=min(max_lines, _CAP_EVENTS)),
            max_lines,
        ),
        "k8s_pod": k8s_pod,
        "k8s_resource_usage": cap_lines(client.get_resource_usage(pod_name), max_lines),
        "plan_results": _plan_results(plan, spec, selected),
    }
