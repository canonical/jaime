# Configuration

This document describes the Juju charm configuration options for **Jaime - Juju AI Medic Engine**.

Jaime ships as a machine subordinate charm (`jaime`) and a Kubernetes
standalone charm (`jaime-k8s`). It observes workloads, collects bounded
diagnostics once a unit becomes unhealthy, and writes a Markdown incident
report with structured audit events. It never remediates. Options are shared
unless marked otherwise, and the defaults below match the shipped `config.yaml`
files.

## Summary

| Config | Type | Default | Description |
|---|---:|---|---|
| `mode` | string | `observe` | `observe`, `suggest`, or `act` (blocked). |
| `provider` | string | `none` | AI provider: `none`, `gemini`, or `openrouter`. |
| `model` | string | empty | Model for the selected provider; empty uses the provider default. |
| `api-token` | string | empty | AI token, as a Juju secret URI (`secret:<id>`) or a plain string. Never logged. |
| `watch-statuses` | string | `error,blocked` | Unit workload statuses that open an incident. |
| `failure-timeout-minutes` | int | `5` | How long a watched status must persist before an incident opens. |
| `cooldown-minutes` | int | `30` | Minimum time before another report for the same unresolved incident. |
| `log-window-minutes` | int | `30` | How far back to collect recent logs. |
| `max-context-lines` | int | `500` | Per-item cap on collected lines, tightened per section. Not a report or prompt total. |
| `report-dir` | string | `/var/log/jaime/reports` | Directory where Markdown reports are written. |
| `audit-log-path` | string | `/var/log/jaime/events.jsonl` | Path to the structured JSONL audit log. |
| `diagnostics` | string | empty | Substrate-specific JSON diagnostics plan. |
| `watch-applications` | string | empty | Applications to monitor in addition to the machine charm's principal. |
| `juju-api-user` | string | empty | Juju user with `read` on the model, used for the controller API. |
| `juju-api-password` | string | empty | Password for `juju-api-user`, as a Juju secret URI or a plain string. Never logged. |

## `mode`

Controls Jaime's operating mode.

- `observe` (default): collect context, write reports and audit events. On the
  machine charm the AI provider is still used once, to generate the diagnostics
  plan when the principal relation is joined.
- `suggest`: as observe, plus a diagnosis. Jaime sends the already-written report
  to the provider and attaches the root-cause description and one suggested
  command to the incident, retrievable with `get-suggestion`. Nothing is
  executed.
- `act`: not implemented. Setting it blocks the charm; no command is ever
  executed.

## `provider`

Selects the AI provider for optional report generation.

- `none` (default): no provider. Jaime still produces a non-AI report from
  collected evidence.
- `gemini`
- `openrouter`

## `model`

The model to use with the selected provider. Any model the provider supports is
accepted. When empty, a provider default is used:

| Provider | Default model |
|---|---|
| `gemini` | `gemini-2.5-flash` |
| `openrouter` | `~deepseek/deepseek-v4-flash-latest` |

Ignored when `provider` is `none`.

## `api-token`

The API token for the configured provider. The recommended form is a Juju
secret, so the token never appears in `juju config` output:

```bash
SECRET_URI=$(juju add-secret jaime-token token=<TOKEN>)
juju grant-secret jaime-token <application>
juju config <application> api-token="${SECRET_URI}"
```

Jaime reads the `token` field from the secret content. A plain string is
accepted for local development only, and is visible in `juju config` output. The
token is never written to logs, audit events, reports or AI prompts.

## `watch-statuses`

Comma-separated unit workload statuses that open an incident. Units on either
substrate are matched.

Default:

```yaml
watch-statuses: error,blocked
```

`waiting` and `maintenance` are usually left out: they are normal during
deployment, relation setup, upgrades and restarts.

## `failure-timeout-minutes`

How long a unit must remain in a watched status before an incident opens.

Default:

```yaml
failure-timeout-minutes: 5
```

When the timeout elapses, Jaime opens an incident (an `incident-start` audit
event), collects bounded context and writes a report. A unit that recovers
before the timeout does not open an incident.

## `cooldown-minutes`

Prevents another report for the same unresolved incident from being generated
too soon, for example when `update-status` runs every few minutes.

Default:

```yaml
cooldown-minutes: 30
```

## `log-window-minutes`

How far back recent logs are collected (unit logs, journal and workload service
logs on the machine charm; pod and container logs on Kubernetes).

Default:

```yaml
log-window-minutes: 30
```

## `max-context-lines`

Per-item cap on collected lines. Some sections apply a tighter cap, for example
socket statistics and firewall rules. It is not a report or prompt total; the
report keeps the bounded evidence, and prompt projection is a later concern.

Default:

```yaml
max-context-lines: 500
```

## `report-dir`

Directory where Markdown reports are written, one file per incident, named after
the incident id:

```text
/var/log/jaime/reports/<incident-id>.md
```

Default: `/var/log/jaime/reports`.

## `audit-log-path`

Path to the structured JSONL audit log, one JSON object per line.

Default: `/var/log/jaime/events.jsonl`.

A representative `incident-start` event:

```json
{"event":"incident-start","unit":"postgresql/0","workload":"error","status_message":"database is not ready","first_seen":"2026-06-21T15:30:00+00:00","status_since":"2026-06-21T15:25:00+00:00","incident_id":"...","timestamp":"2026-06-21T15:30:00+00:00"}
```

`list-incidents` correlates `incident-start`, `report-generated` and
`incident-closed` events by incident id.

## `watch-applications`

Applications to monitor **in addition to** what the charm watches by default.
The reach differs per substrate.

On the machine charm the related principal is always monitored, whatever this
option says. Other units are matched only on the **same host**:

| Value | Monitors | Credentials |
|---|---|---|
| `""` (default) | the principal only | not needed |
| `app1,app2` | the principal, plus co-located units of those applications | required |
| `*` | the principal, plus every co-located unit | required |

On the Kubernetes charm there is no principal relation: monitoring is opt-in,
there is no `*`, and the reach is any application in the model's namespace:

| Value | Monitors | Credentials |
|---|---|---|
| `""` (default) | nothing | not needed, but the charm is not usable |
| `app1,app2` | those applications | required |

A configured application that cannot be reached is skipped on the machine charm
and blocks the Kubernetes charm with a clear status.

## `juju-api-user`

Name of a Juju user with `read` permission on the model, used to read workload
statuses through the controller API. A unit's own agent identity lacks the
`ModelRead` permission that `Client.FullStatus` requires.

```bash
juju add-user jaime-observer
juju grant jaime-observer read <model-name>
```

The Kubernetes charm always requires it. The machine charm requires it only when
`watch-applications` is non-empty; with it, the principal's status message is
also captured.

## `juju-api-password`

Password for `juju-api-user`. As with `api-token`, the recommended form is a
Juju secret:

```bash
SECRET_URI=$(juju add-secret jaime-juju-api password=<PASSWORD>)
juju grant-secret jaime-juju-api jaime
juju config jaime juju-api-password="${SECRET_URI}"
```

Jaime reads the `password` field from the secret content. A plain string is
accepted for local development but is visible in `juju config` output. The
password is never written to logs, audit events, reports or AI prompts.

## `diagnostics`

A JSON diagnostics plan. The format differs per substrate.

**Machine subordinate.** A plan describing what to collect on the host (log
files, processes, environment variables, network ports, systemd units, health
commands). When empty, Jaime attempts to generate a plan via AI on
relation-joined, and falls back to an empty plan if no provider is configured.
The schema is `DIAGNOSTICS_SCHEMA` in `jaime-package/jaime/diagnostics.py`.

**Kubernetes standalone.** An object keyed by application name. Each value may
set `containers`, `log_patterns`, `env_variables` and `ports`, checked against
what the Kubernetes API can observe without exec into the workload:

```json
{
  "postgresql-k8s": {
    "containers": ["postgresql"],
    "log_patterns": ["FATAL", "out of memory"],
    "env_variables": ["PGDATA", "POSTGRES_PASSWORD"],
    "ports": [{"port": 5432, "protocol": "tcp"}]
  }
}
```

`containers` restricts which containers are collected (all by default) and
reports any named container the pod lacks. `log_patterns` keeps matching lines
in addition to the error/warning filter. `env_variables` checks names only;
values are never collected. `ports` checks that a port is declared in the pod
spec, not that anything is listening. When empty, the fixed pod collection is
used. An invalid plan blocks the unit. The validator is
`validate_k8s_diagnostics` in `jaime-package/jaime/diagnostics.py`.

## Secret redaction

Incident reports redact secrets before they are written, so the persisted
report and the prompt built from it contain none. Config options whose name
marks them sensitive (`password`, `token`, `secret`, `credential`, and the
`api-key`/`private-key`/`secret-key`/`access-key` forms) keep their name and
show `[REDACTED]` instead of the value; a Juju `type: secret` option shows set
or unset. Recognisable secrets in logs, health-command output, snap logs and the
status message (Juju secret URIs, `Bearer` tokens, private-key blocks, JWTs, AWS
access keys, and `password=`/`token:`-style assignments) are replaced with
`[REDACTED]`.

The policy is deliberately conservative: it does not guess at high-entropy
strings, so commit SHAs, UUIDs and versions are left intact. The audit log and
the persisted `status-state.json` are not redacted; do not put secrets in a
workload status message.

## Example configuration

```yaml
mode: observe
provider: none
watch-statuses: error,blocked
failure-timeout-minutes: 5
cooldown-minutes: 30
log-window-minutes: 30
max-context-lines: 500
report-dir: /var/log/jaime/reports
audit-log-path: /var/log/jaime/events.jsonl
diagnostics: ""
watch-applications: ""
juju-api-user: ""
juju-api-password: ""
```
