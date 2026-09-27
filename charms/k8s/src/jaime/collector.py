"""Kubernetes context collection for Jaime K8s incidents.

Collects bounded diagnostics from the Kubernetes API without modifying any
state. All collection is read-only and bounded by time and line count.

Implements the same collect_context() interface as the machine collector,
so charm.py and report.py remain substrate-agnostic.
"""

import datetime
import logging

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


def collect_context(
    unit_name: str,
    log_window_minutes: int = _DEFAULT_LOG_WINDOW_MINUTES,
    max_lines: int = _DEFAULT_MAX_LINES,
    from_time: datetime.datetime | None = None,
    diagnostics_plan: dict | None = None,
) -> dict:
    """Collect bounded diagnostic context for a Kubernetes unit.

    ``unit_name`` is a Juju unit name (e.g. ``postgresql-k8s/0``); the pod is
    resolved via the ``unit.juju.is/id`` annotation.

    Returns a context dict compatible with report.py, plus k8s-specific keys
    (``k8s_pod``, ``k8s_events``, ``k8s_resource_usage``).
    """
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
        }

    metadata = pod.get("metadata", {})
    pod_name = metadata.get("name", "")
    spec = pod.get("spec", {})
    status = pod.get("status", {})

    # Logs from every container in the pod. Fetch a wide window (tailLines
    # always returns the END of the window), then keep only error/warning
    # lines with context, so the causal error at the start of the window is
    # never pushed out by later noise. Finally deduplicate so repeated
    # failures (health checks firing every few seconds) collapse.
    unit_logs = []
    for container in spec.get("containers", []):
        logs = client.get_pod_logs(
            pod_name,
            container=container.get("name"),
            since_time=from_time,
            tail_lines=_FETCH_LINES_CAP,
        )
        if logs:
            logs = cap_lines(deduplicate_lines(filter_error_context(logs, max_lines)), max_lines)
            unit_logs.append(f"=== container: {container.get('name')} ===")
            unit_logs.extend(logs)
    unit_logs = cap_lines(unit_logs, max_lines)

    # Previous-instance logs explain a crash loop; only for containers that
    # have actually restarted, and only the first few of them.
    previous_logs = []
    started = 0
    for cs in status.get("containerStatuses", []):
        if started >= _CAP_PREVIOUS_CONTAINERS:
            break
        if cs.get("restartCount", 0) <= 0:
            continue
        logs = client.get_pod_logs(
            pod_name, container=cs.get("name"), previous=True,
            tail_lines=_FETCH_LINES_CAP,
        )
        if logs:
            started += 1
            logs = cap_lines(deduplicate_lines(filter_error_context(logs, max_lines)), max_lines)
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
    }
