"""Unit tests for jaime.k8s_api and jaime.collector (k8s variant)."""

import datetime
import unittest.mock as mock

from jaime.collector import collect_context
from jaime.k8s_api import K8sApiClient


def _make_client():
    """Return a K8sApiClient with filesystem reads mocked out."""
    with mock.patch("builtins.open", mock.mock_open(read_data="tok")):
        client = K8sApiClient.__new__(K8sApiClient)
        client._token = "tok"
        client.namespace = "test-ns"
        client._ca_path = "/fake/ca.crt"
        client._timeout = 15
        return client


class TestK8sApiClient:
    def test_list_pods_returns_items(self):
        client = _make_client()
        with mock.patch.object(client, "_request", return_value={
            "items": [{"metadata": {"name": "app-0"}}]
        }):
            pods = client.list_pods()
        assert pods == [{"metadata": {"name": "app-0"}}]

    def test_list_pods_empty_on_error(self):
        client = _make_client()
        with mock.patch.object(client, "_request", return_value=None):
            assert client.list_pods() == []

    def test_get_pod_for_unit_matches_annotation(self):
        client = _make_client()
        pods = [
            {"metadata": {"name": "other-0", "annotations": {"unit.juju.is/id": "other/0"}}},
            {"metadata": {"name": "postgresql-k8s-0", "annotations": {"unit.juju.is/id": "postgresql-k8s/0"}}},
        ]
        with mock.patch.object(client, "list_pods", return_value=pods):
            pod = client.get_pod_for_unit("postgresql-k8s/0")
        assert pod["metadata"]["name"] == "postgresql-k8s-0"

    def test_get_pod_for_unit_falls_back_to_name_convention(self):
        client = _make_client()
        pods = [
            {"metadata": {"name": "postgresql-k8s-0", "annotations": {}}},
        ]
        with mock.patch.object(client, "list_pods", return_value=pods):
            pod = client.get_pod_for_unit("postgresql-k8s/0")
        assert pod["metadata"]["name"] == "postgresql-k8s-0"

    def test_get_pod_for_unit_returns_none_when_missing(self):
        client = _make_client()
        with mock.patch.object(client, "list_pods", return_value=[]):
            assert client.get_pod_for_unit("app/0") is None

    def test_get_pod_logs_bounds_by_tail_lines(self):
        client = _make_client()
        captured = {}

        def fake_request(path, params=None):
            captured["path"] = path
            captured["params"] = params
            return "line1\nline2\nline3"

        with mock.patch.object(client, "_request", side_effect=fake_request):
            logs = client.get_pod_logs("app-0", container="workload", tail_lines=3)
        assert logs == ["line1", "line2", "line3"]
        assert captured["params"]["tailLines"] == 3
        assert captured["params"]["container"] == "workload"

    def test_get_pod_logs_includes_since_time(self):
        client = _make_client()
        captured = {}

        def fake_request(path, params=None):
            captured["params"] = params
            return ""

        since = datetime.datetime(2026, 8, 18, 10, 0, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(client, "_request", side_effect=fake_request):
            client.get_pod_logs("app-0", since_time=since)
        assert captured["params"]["sinceTime"] == "2026-08-18T10:00:00Z"

    def test_get_pod_logs_empty_on_error(self):
        client = _make_client()
        with mock.patch.object(client, "_request", return_value=None):
            assert client.get_pod_logs("app-0") == []

    def test_get_pod_events_formats_lines(self):
        client = _make_client()
        with mock.patch.object(client, "_request", return_value={
            "items": [
                {"type": "Warning", "reason": "BackOff",
                 "message": "Back-off restarting failed container",
                 "lastTimestamp": "2026-08-18T10:05:00Z"},
            ],
        }):
            events = client.get_pod_events("app-0")
        assert len(events) == 1
        assert "BackOff" in events[0]
        assert "Warning" in events[0]

    def test_get_pod_events_handles_none_timestamps(self):
        """Events with a missing/None lastTimestamp must not break sorting."""
        client = _make_client()
        with mock.patch.object(client, "_request", return_value={
            "items": [
                {"type": "Warning", "reason": "Scheduled",
                 "message": "Successfully assigned", "lastTimestamp": None},
                {"type": "Normal", "reason": "Pulled",
                 "message": "Container image pulled",
                 "firstTimestamp": "2026-08-18T10:00:00Z"},
            ],
        }):
            events = client.get_pod_events("app-0")
        assert len(events) == 2
        assert any("Scheduled" in e for e in events)
        assert any("Pulled" in e for e in events)

    def test_get_resource_usage_empty_without_metrics_server(self):
        client = _make_client()
        with mock.patch.object(client, "_request", return_value=None):
            assert client.get_resource_usage("app-0") == []


class TestCheckAccess:
    """The RBAC preflight (TASKS 4.2) behind "Kubernetes API readable"."""

    def _raw(self, *codes):
        """A _request_raw replacement yielding one result per call."""
        calls = iter(codes)
        return mock.Mock(side_effect=lambda *a, **k: next(calls))

    def test_ok_when_pods_and_logs_readable(self):
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (200, "{}", "application/json"),
            (200, "log", "text/plain"),
        )), mock.patch.object(client, "list_pods", return_value=[
            {"metadata": {"name": "app-0"}}
        ]):
            assert client.check_access() == ("ok", None)

    def test_forbidden_when_pod_list_denied(self):
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (403, "Forbidden", "application/json"),
        )):
            kind, detail = client.check_access()
        assert kind == "forbidden"
        assert "list pods" in detail

    def test_forbidden_when_pod_logs_denied(self):
        """The shipped RoleBinding is needed for logs; default SA gets 403."""
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (200, "{}", "application/json"),
            (403, "Forbidden", "text/plain"),
        )), mock.patch.object(client, "list_pods", return_value=[
            {"metadata": {"name": "app-0"}}
        ]):
            kind, detail = client.check_access()
        assert kind == "forbidden"
        assert "pod logs" in detail

    def test_unreachable_on_transport_error(self):
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (None, None, None),
        )):
            kind, detail = client.check_access()
        assert kind == "unreachable"

    def test_unreachable_on_unexpected_status(self):
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (503, "Service Unavailable", "text/plain"),
        )):
            kind, detail = client.check_access()
        assert kind == "unreachable"

    def test_ok_when_no_pods_to_probe_logs(self):
        """An empty namespace cannot test logs, but the API itself is fine."""
        client = _make_client()
        with mock.patch.object(client, "_request_raw", self._raw(
            (200, "{}", "application/json"),
        )), mock.patch.object(client, "list_pods", return_value=[]):
            assert client.check_access() == ("ok", None)


_POD = {
    "metadata": {"name": "postgresql-k8s-0", "annotations": {"unit.juju.is/id": "postgresql-k8s/0"}},
    "spec": {
        "nodeName": "node-1",
        "containers": [
            {"name": "charm", "image": "jujusolutions/charm:3.6"},
            {
                "name": "postgresql",
                "image": "postgres:16",
                "resources": {
                    "requests": {"cpu": "500m", "memory": "1Gi"},
                    "limits": {"cpu": "2", "memory": "4Gi"},
                },
                "livenessProbe": {
                    "httpGet": {"path": "/health", "port": 8008},
                    "initialDelaySeconds": 30,
                },
                "readinessProbe": {
                    "exec": {"command": ["pg_isready"]},
                    "initialDelaySeconds": 10,
                },
                "volumeMounts": [
                    {"name": "config-volume", "mountPath": "/etc/postgresql"},
                    {"name": "certs", "mountPath": "/certs"},
                ],
            },
        ],
        "volumes": [
            {"name": "config-volume", "configMap": {"name": "postgresql-k8s-config"}},
            {"name": "certs", "secret": {"secretName": "postgresql-k8s-certificates"}},
            {"name": "data", "persistentVolumeClaim": {"claimName": "pgdata"}},
        ],
    },
    "status": {
        "phase": "Running",
        "podIP": "10.1.0.99",
        "qosClass": "Burstable",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [
            {"name": "charm", "image": "jujusolutions/charm:3.6", "ready": True,
             "restartCount": 0, "state": {"running": {}}},
            {"name": "postgresql", "image": "postgres:16", "ready": False,
             "restartCount": 3, "state": {"waiting": {}}},
        ],
    },
}


class TestCollectContext:
    def test_returns_all_sections(self):
        with mock.patch("jaime.collector.K8sApiClient") as mock_cls:
            client = mock_cls.return_value
            client.get_pod_for_unit.return_value = _POD
            client.get_pod_logs.return_value = ["log line"]
            client.get_pod_events.return_value = ["event line"]
            client.get_resource_usage.return_value = ["postgresql: cpu=100m memory=256Mi"]

            ctx = collect_context("postgresql-k8s/0")

        assert "collected_at" in ctx
        assert "unit_logs" in ctx
        assert "k8s_events" in ctx
        assert "k8s_pod" in ctx
        assert "k8s_resource_usage" in ctx
        assert ctx["k8s_pod"]["name"] == "postgresql-k8s-0"
        assert ctx["k8s_pod"]["phase"] == "Running"

    def test_collects_logs_per_container(self):
        with mock.patch("jaime.collector.K8sApiClient") as mock_cls:
            client = mock_cls.return_value
            client.get_pod_for_unit.return_value = _POD
            client.get_pod_logs.return_value = ["some log"]
            client.get_pod_events.return_value = []
            client.get_resource_usage.return_value = []

            ctx = collect_context("postgresql-k8s/0")

        # Two containers in the pod → two calls, two headers
        assert client.get_pod_logs.call_count == 2
        assert ctx["unit_logs"].count("=== container: charm ===") == 1
        assert ctx["unit_logs"].count("=== container: postgresql ===") == 1

    def test_missing_pod_returns_empty_context(self):
        with mock.patch("jaime.collector.K8sApiClient") as mock_cls:
            mock_cls.return_value.get_pod_for_unit.return_value = None
            ctx = collect_context("unknown/0")
        assert ctx["unit_logs"] == []
        assert ctx["k8s_pod"] == {}

    def test_container_summary_captures_restarts(self):
        with mock.patch("jaime.collector.K8sApiClient") as mock_cls:
            client = mock_cls.return_value
            client.get_pod_for_unit.return_value = _POD
            client.get_pod_logs.return_value = []
            client.get_pod_events.return_value = []
            client.get_resource_usage.return_value = []

            ctx = collect_context("postgresql-k8s/0")

        pg = next(c for c in ctx["k8s_pod"]["containers"] if c["name"] == "postgresql")
        assert pg["restartCount"] == 3
        assert pg["ready"] is False
        assert pg["state"] == "waiting"

    def test_pod_spec_details(self):
        with mock.patch("jaime.collector.K8sApiClient") as mock_cls:
            client = mock_cls.return_value
            client.get_pod_for_unit.return_value = _POD
            client.get_pod_logs.return_value = []
            client.get_pod_events.return_value = []
            client.get_resource_usage.return_value = []

            ctx = collect_context("postgresql-k8s/0")

        pod = ctx["k8s_pod"]
        assert pod["node"] == "node-1"
        assert pod["pod_ip"] == "10.1.0.99"
        assert pod["qos"] == "Burstable"

        pg = next(c for c in pod["containers"] if c["name"] == "postgresql")
        assert "limits: cpu=2, memory=4Gi" in pg["resources"]
        assert "requests: cpu=500m, memory=1Gi" in pg["resources"]
        assert "httpGet /health :8008 (delay 30s)" == pg["liveness"]
        assert "exec `pg_isready` (delay 10s)" == pg["readiness"]

        # charm container has no resources/probes
        charm = next(c for c in pod["containers"] if c["name"] == "charm")
        assert charm["resources"] == ""
        assert charm["liveness"] == ""

        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["config-volume"]["source"] == "configMap/postgresql-k8s-config"
        assert volumes["config-volume"]["mounts"] == ["/etc/postgresql"]
        assert volumes["certs"]["source"] == "secret/postgresql-k8s-certificates"
        assert volumes["certs"]["mounts"] == ["/certs"]
        assert volumes["data"]["source"] == "pvc/pgdata"
        assert volumes["data"]["mounts"] == []
