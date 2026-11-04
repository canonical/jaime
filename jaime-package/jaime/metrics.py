"""Prometheus metrics for Jaime's own operation (incidents and AI usage).

The renderer has no Juju dependency: it draws from the incident tracker and
the usage ledger that the charms already keep in memory and on disk, so the
same code can serve them from a hook or from a long-lived metrics process.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jaime.actions import summarise_usage

logger = logging.getLogger(__name__)

METRICS_PORT = 7800
"""Default port jaime serves ``/metrics`` on, advertised via ``cos-agent``."""


def _escape_label(value: str) -> str:
    """Escape a value for use inside a Prometheus ``{label="..."}`` value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_metrics(tracker) -> str:
    """Render jaime's Prometheus text-format metrics from a ``StatusTracker``.

    Emits, per monitored unit with an open incident:

    - ``jaime_incidents_open{unit}`` gauge (1)

    and, per model from the usage ledger:

    - ``jaime_llm_calls_total{model}`` counter
    - ``jaime_llm_tokens_total{model,role}`` counter, role in prompt|completion
    - ``jaime_llm_cost_total{model}`` counter (USD, when the provider reports it)
    """
    lines: list[str] = []

    lines.append("# HELP jaime_incidents_open Incidents currently open.")
    lines.append("# TYPE jaime_incidents_open gauge")
    for unit, state in tracker.entries():
        incident = state.get("incident") or {}
        if incident and not incident.get("closed_at"):
            lines.append(f'jaime_incidents_open{{unit="{_escape_label(unit)}"}} 1')

    lines.append("# HELP jaime_llm_calls_total LLM calls made.")
    lines.append("# TYPE jaime_llm_calls_total counter")
    lines.append("# HELP jaime_llm_tokens_total Tokens used by role.")
    lines.append("# TYPE jaime_llm_tokens_total counter")
    lines.append("# HELP jaime_llm_cost_total Aggregated model cost in USD.")
    lines.append("# TYPE jaime_llm_cost_total counter")

    summary = summarise_usage(tracker.all_usage_log(), tracker.folded_usage())
    for model, m in sorted(summary.get("by_model", {}).items()):
        label = _escape_label(model)
        lines.append(f'jaime_llm_calls_total{{model="{label}"}} {m["calls"]}')
        lines.append(f'jaime_llm_tokens_total{{model="{label}",role="prompt"}} {m["prompt_tokens"]}')
        lines.append(f'jaime_llm_tokens_total{{model="{label}",role="completion"}} {m["completion_tokens"]}')
        cost = m.get("cost_usd")
        if cost is not None:
            lines.append(f'jaime_llm_cost_total{{model="{label}"}} {cost}')

    return "\n".join(lines) + "\n"


def _make_handler(render):
    class Handler(BaseHTTPRequestHandler):
        """A minimal ``/metrics`` handler that delegates to a render callable."""

        def do_GET(self):  # noqa: N802 (http.server API)
            body = self._body_for(self.path)
            self.send_response(*body[0])
            self.send_header("Content-Type", body[1])
            self.send_header("Content-Length", str(len(body[2])))
            self.end_headers()
            self.wfile.write(body[2])

        def _body_for(self, path: str):
            if path == "/metrics":
                return (200, "text/plain; version=0.0.4; charset=utf-8", render().encode())
            if path in ("/", "/-/healthy"):
                return (200, "text/plain; charset=utf-8", b"ok\n")
            return (404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
            logger.debug("metrics server: %s", format % args)

    return Handler


def create_metrics_server(render, *, host: str = "0.0.0.0", port: int = METRICS_PORT):
    """Return a threaded HTTP server serving ``/metrics`` from ``render``."""
    server = ThreadingHTTPServer((host, port), _make_handler(render))
    server.daemon_threads = True
    return server
