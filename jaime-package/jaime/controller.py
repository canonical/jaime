"""Minimal Juju controller API client (stdlib + websocket-client).

Connects to the Juju controller WebSocket API, authenticates with a Juju
user account, and fetches FullStatus so the charm can monitor workload
statuses of other applications in the model.

The unit's own agent.conf (/var/lib/juju/agents/unit-*/agent.conf) contains
the controller address, CA certificate, and model UUID — this client derives
those automatically. Only the Juju user credentials (name + password) need
to be supplied by the operator, since a unit's own agent identity does not
have ModelRead permission required by the Client.FullStatus facade call.

Protocol: JSON-RPC-style envelopes over a single WebSocket connection.

    Request:  {"type": facade, "request": method, "version": int,
               "params": {...}, "request-id": int}
    Response: {"request-id": int, "response": {...}}
           or {"request-id": int, "error": str, "error-code": str}
"""

import json
import logging
import os
import re
import tempfile

import websocket

logger = logging.getLogger(__name__)


class ControllerError(Exception):
    """Raised when the controller API returns an error or is unreachable."""


class ControllerAuthError(ControllerError):
    """Raised when authentication with the controller fails."""


def agent_conf_path(unit_name: str | None = None) -> str | None:
    """Return the path to the charm's own agent.conf, or None if absent."""
    unit = unit_name or os.environ.get("JUJU_UNIT_NAME", "")
    if not unit:
        return None
    dirname = "unit-" + unit.replace("/", "-")
    path = f"/var/lib/juju/agents/{dirname}/agent.conf"
    return path if os.path.exists(path) else None


def parse_agent_conf(path: str) -> dict:
    """Extract controller connection details from a Juju agent.conf file.

    Returns a dict with keys: api_address, ca_cert, model_uuid.
    """
    with open(path) as f:
        content = f.read()

    m = re.search(r"cacert: \|\n((?:.*\n)*?)(?=\S)", content)
    if not m:
        raise ControllerError("cacert not found in agent.conf")
    ca_cert = "\n".join(
        line[2:] if line.startswith("  ") else line
        for line in m.group(1).splitlines()
    ).strip() + "\n"

    addr = re.search(r"apiaddresses:\n- (.*)", content)
    if not addr:
        raise ControllerError("apiaddresses not found in agent.conf")

    model = re.search(r"^model: (.*)$", content, re.MULTILINE)
    if not model:
        raise ControllerError("model not found in agent.conf")

    return {
        "api_address": addr.group(1).strip(),
        "ca_cert": ca_cert,
        "model_uuid": model.group(1).strip().replace("model-", "", 1),
    }


class JujuControllerClient:
    """WebSocket client for the Juju controller API."""

    def __init__(self, api_address: str, ca_cert: str, model_uuid: str,
                 timeout: int = 30):
        self._url = f"wss://{api_address}/model/{model_uuid}/api"
        self._ca_cert = ca_cert
        self._timeout = timeout
        self._ws = None
        self._ca_path = None
        self._request_id = 0
        self.facades: dict[str, list[int]] = {}

    def __enter__(self) -> "JujuControllerClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> None:
        """Open the WebSocket connection with TLS verification."""
        with tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False) as f:
            f.write(self._ca_cert)
            self._ca_path = f.name
        try:
            self._ws = websocket.create_connection(
                self._url,
                sslopt={"ca_certs": self._ca_path},
                timeout=self._timeout,
            )
        except Exception as e:
            raise ControllerError(f"could not connect to {self._url}: {e}") from e

    def _call(self, facade: str, method: str, version: int,
              params: dict | None = None) -> dict:
        """Send one JSON-RPC request and return the response payload."""
        self._request_id += 1
        self._ws.send(json.dumps({
            "type": facade,
            "request": method,
            "version": version,
            "params": params or {},
            "request-id": self._request_id,
        }))
        response = json.loads(self._ws.recv())
        if response.get("error"):
            raise ControllerError(
                f"{facade}.{method}: {response['error']} "
                f"(code: {response.get('error-code', 'unknown')})"
            )
        return response.get("response", {})

    def login(self, username: str, password: str) -> dict:
        """Authenticate with the controller and record available facades."""
        if not username.startswith("user-"):
            username = f"user-{username}"
        try:
            response = self._call("Admin", "Login", 3, {
                "auth-tag": username,
                "credentials": password,
            })
        except ControllerError as e:
            raise ControllerAuthError(str(e)) from e
        for entry in response.get("facades", []):
            self.facades[entry["name"]] = entry["versions"]
        return response

    def facade_version(self, name: str, default: int) -> int:
        """Return the highest available version of a facade we support."""
        versions = self.facades.get(name)
        if not versions:
            return default
        return max(versions)

    def full_status(self) -> dict:
        """Fetch FullStatus for the model (requires ModelRead permission)."""
        version = self.facade_version("Client", 5)
        return self._call("Client", "FullStatus", version, {"patterns": []})

    def application_get(self, application: str) -> dict:
        """Fetch an application's current config via Application.Get.

        Returns the full response dict. The ``config`` key maps option names
        to ``{default, description, source, type, value}`` — where
        ``source == "user"`` marks options the operator explicitly changed.
        """
        version = self.facade_version("Application", 15)
        return self._call("Application", "Get", version,
                          {"application": application})

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._ca_path:
            try:
                os.unlink(self._ca_path)
            except OSError:
                pass
            self._ca_path = None


def extract_unit_statuses(full_status: dict,
                          watch_applications: list[str] | None = None,
                          exclude_applications: list[str] | None = None,
                          ) -> dict[str, dict]:
    """Extract per-unit workload statuses from a FullStatus response.

    Returns {unit_name: {"status": str, "since": str, "message": str,
    "machine": str}}.

    Subordinate units are nested under their principal unit's ``subordinates``
    map rather than appearing in their own application's ``units`` (which is
    empty for a subordinate app), so they are walked recursively. Juju reports
    a subordinate's own ``machine`` as an empty string, so it inherits its
    principal's machine, which is what makes host filtering work.

    The watch and exclude filters are applied per unit, by the application the
    unit name belongs to. Filtering only at the application level would either
    leak every subordinate of a watched principal or hide a watched
    subordinate that happens to sit under an unwatched principal.

    If watch_applications is empty/None, all applications are included.
    """
    watch = {a for a in (watch_applications or []) if a}
    exclude = set(exclude_applications or [])
    result: dict[str, dict] = {}

    def walk(unit_name: str, unit_data: dict, parent_machine: str) -> None:
        app_name = unit_name.split("/")[0]
        machine = unit_data.get("machine") or parent_machine
        if app_name not in exclude and (not watch or app_name in watch):
            ws = unit_data.get("workload-status") or {}
            result[unit_name] = {
                "status": ws.get("status", "unknown"),
                "since": ws.get("since", ""),
                "message": ws.get("info", ""),
                "machine": machine,
            }
        # Recurse even when this unit is filtered out: a watched subordinate
        # can be nested under a principal we are not watching.
        for sub_name, sub_data in (unit_data.get("subordinates") or {}).items():
            walk(sub_name, sub_data, machine)

    for _app_name, app_data in (full_status.get("applications") or {}).items():
        for unit_name, unit_data in (app_data.get("units") or {}).items():
            walk(unit_name, unit_data, "")
    return result
