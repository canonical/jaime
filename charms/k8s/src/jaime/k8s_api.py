"""Kubernetes API client using the in-cluster service account (stdlib only).

Every Juju k8s charm pod already mounts a service account token at
/var/run/secrets/kubernetes.io/serviceaccount/. This client uses that token
to talk directly to the Kubernetes API server — no kubectl binary required.

The default Juju-created service account (named after the application) can
already get/list pods in the model namespace. Reading pod logs requires an
additional Role/RoleBinding granting `get` on `pods/log` — all failures are
handled gracefully and logged at debug level.
"""

import datetime
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_API_BASE = "https://kubernetes.default.svc"


class K8sApiClient:
    """Read-only Kubernetes API client for the pod's own namespace."""

    def __init__(self, sa_dir: str = _SA_DIR, timeout: int = 15):
        self._token = open(f"{sa_dir}/token").read().strip()
        self.namespace = open(f"{sa_dir}/namespace").read().strip()
        self._ca_path = f"{sa_dir}/ca.crt"
        self._timeout = timeout

    def _request_raw(self, path: str, params: dict | None = None):
        """GET from the API server, returning (status, body, content_type).

        Unlike ``_request`` this does not swallow the HTTP status code: the
        RBAC preflight needs to distinguish ``403 Forbidden`` (missing
        RoleBinding) from a transport failure ``(None, ...)``. Any HTTP
        response is returned as ``(status, body, content_type)``; a request
        that never gets a response returns ``(None, None, None)``.
        """
        url = f"{_API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}"}
        )
        ctx = ssl.create_default_context(cafile=self._ca_path)
        try:
            with urllib.request.urlopen(req, context=ctx,
                                        timeout=self._timeout) as resp:
                return (
                    resp.status,
                    resp.read().decode(errors="replace"),
                    resp.headers.get("Content-Type", ""),
                )
        except urllib.error.HTTPError as e:
            logger.debug("k8s API %s returned HTTP %s", path, e.code)
            return e.code, e.read().decode(errors="replace"), ""
        except Exception as e:
            logger.debug("k8s API %s failed: %s", path, e)
            return None, None, None

    def _request(self, path: str, params: dict | None = None) -> dict | str | None:
        """GET from the API server. Returns parsed JSON, raw text, or None."""
        status, body, content_type = self._request_raw(path, params)
        if status is None or status != 200 or body is None:
            return None
        if "application/json" in content_type:
            try:
                return json.loads(body)
            except ValueError:
                return body
        return body

    def check_access(self) -> tuple[str, str | None]:
        """Verify the in-cluster service account has the reads collection needs.

        Returns ``("ok", None)`` when everything is readable, or
        ``("forbidden"|"unreachable", detail)`` otherwise. ``forbidden`` is an
        RBAC problem the operator must fix (the RoleBinding is missing);
        ``unreachable`` is a transient connectivity/server problem. This is
        the explicit preflight behind TASKS 4.2's "Kubernetes API readable".
        """
        # The default Juju service account can list pods; a 403 here means the
        # model namespace itself is unreachable for the account.
        status, _, _ = self._request_raw(f"/api/v1/namespaces/{self.namespace}/pods")
        if status is None:
            return "unreachable", "cannot reach the Kubernetes API"
        if status == 403:
            return "forbidden", "cannot list pods (RoleBinding not applied)"
        if status != 200:
            return "unreachable", f"unexpected HTTP {status} listing pods"

        # Pod logs are the high-value read; the default account cannot do it
        # without the shipped RoleBinding. Probe the first pod in the
        # namespace (Jaime's own pod is always there).
        pods = self.list_pods()
        if pods:
            first = pods[0]["metadata"]["name"]
            status, _, _ = self._request_raw(
                f"/api/v1/namespaces/{self.namespace}/pods/{first}/log",
                {"tailLines": 1},
            )
            if status is None:
                return "unreachable", "cannot read pod logs"
            if status == 403:
                return "forbidden", (
                    "cannot read pod logs (apply jaime-k8s-rbac.yaml)"
                )
            if status != 200:
                return "unreachable", f"unexpected HTTP {status} reading pod logs"
        return "ok", None

    def list_pods(self) -> list[dict]:
        """Return all pods in the model namespace."""
        data = self._request(f"/api/v1/namespaces/{self.namespace}/pods")
        return data.get("items", []) if isinstance(data, dict) else []

    def get_pod_for_unit(self, unit_name: str) -> dict | None:
        """Find the pod for a Juju unit via the unit.juju.is/id annotation."""
        for pod in self.list_pods():
            annotations = pod.get("metadata", {}).get("annotations", {})
            if annotations.get("unit.juju.is/id") == unit_name:
                return pod
        # Fallback: name convention app-N
        conventional = unit_name.replace("/", "-") if "/" in unit_name else unit_name
        for pod in self.list_pods():
            if pod.get("metadata", {}).get("name") == conventional:
                return pod
        return None

    def get_pod_logs(
        self,
        pod_name: str,
        container: str | None = None,
        since_time: datetime.datetime | None = None,
        tail_lines: int = 500,
    ) -> list[str]:
        """Return pod log lines, bounded by time and line count."""
        params = {"tailLines": tail_lines}
        if container:
            params["container"] = container
        if since_time:
            params["sinceTime"] = since_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        path = f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}/log"
        result = self._request(path, params)
        if not isinstance(result, str):
            return []
        return [line.rstrip() for line in result.splitlines() if line.strip()]

    def get_pod_events(self, pod_name: str, limit: int = 50) -> list[str]:
        """Return Kubernetes events for a pod as formatted text lines."""
        path = f"/api/v1/namespaces/{self.namespace}/events"
        data = self._request(path, {
            "fieldSelector": f"involvedObject.name={pod_name}",
            "limit": limit,
        })
        if not isinstance(data, dict):
            return []
        lines = []
        for event in sorted(
            data.get("items", []),
            key=lambda e: (
                e.get("lastTimestamp")
                or e.get("firstTimestamp")
                or ""
            ),
        ):
            ts = event.get("lastTimestamp") or event.get("firstTimestamp") or ""
            lines.append(
                f"{ts} {event.get('type', '')} {event.get('reason', '')} "
                f"{event.get('message', '')}"
            )
        return lines

    def get_resource_usage(self, pod_name: str) -> list[str]:
        """Return CPU/memory usage from metrics-server, if available."""
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.namespace}/pods/{pod_name}"
        data = self._request(path)
        if not isinstance(data, dict):
            return []
        lines = []
        for container in data.get("containers", []):
            usage = container.get("usage", {})
            lines.append(
                f"{container.get('name', '?')}: "
                f"cpu={usage.get('cpu', '?')} memory={usage.get('memory', '?')}"
            )
        return lines
