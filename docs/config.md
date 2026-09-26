# Configuration

This document describes the Juju charm configuration options for **Jaime — Juju AI Medic Engine**.

Jaime is currently designed as an **observe-first machine subordinate charm**. The phase-1 MVP should detect unhealthy principal charm states, collect local diagnostic context, and generate structured incident information. It should not remediate or mutate the host unless a future task explicitly adds that behaviour.

## Summary

| Config | Type | Default | Description |
|---|---:|---|---|
| `mode` | string | `observe` | Controls whether Jaime only observes or may later act. For phase-1, only `observe` is implemented. |
| `provider` | string | `none` | AI provider to use for optional report generation. For phase-1 this may be `none` or a stub. |
| `model` | string | empty | AI model name used by the selected provider. Not required for non-AI reports. |
| `api-token` | secret | empty | Juju secret containing the provider API token. Must never be logged. |
| `watch-statuses` | string | `error,blocked` | Comma-separated principal unit statuses that should open an incident. |
| `failure-timeout-minutes` | int | `5` | How long a watched status must persist before a report is generated. |
| `cooldown-minutes` | int | `30` | Minimum time before generating another report for the same unresolved incident. |
| `log-window-minutes` | int | `30` | How far back Jaime should collect recent logs. |
| `max-context-lines` | int | `500` | Per-item cap on collected lines. Some sections apply a tighter cap (for example socket statistics and firewall rules). This is not a report or prompt total. |
| `report-dir` | string | `/var/log/jaime/reports` | Directory where Markdown or JSON report artifacts are written. |
| `audit-log-path` | string | `/var/log/jaime/events.jsonl` | Path to the structured JSONL audit log. |
| `diagnostics` | string | empty | Machine only. JSON monitoring plan; empty means generate one via AI on relation-joined. |
| `watch-applications` | string | empty | Applications whose co-located units to watch, in addition to the always-watched principal. `*` means every co-located unit. |
| `juju-api-user` | string | empty | Juju user with `read` on the model, used for the controller API. Required only when `watch-applications` is non-empty. |
| `juju-api-password` | secret | empty | Password for `juju-api-user`; a Juju secret URI (`secret:<id>`) or a plain string (development only). Never logged. |

## `mode`

Controls Jaime's operating mode.

Allowed values:

- `observe`
- `act` — reserved for a later phase

Default:

```yaml
mode: observe
```

Phase-1 behaviour:

- `observe` only
- no remediation
- no host mutation
- collect state
- write structured logs
- generate diagnostic output

`act` should be documented but not implemented until remediation has explicit safety rules, allowlists, tests, and acceptance criteria.

## `provider`

Selects the AI provider for optional report generation.

Suggested values:

- `none`
- `gemini`
- `openai`

Default:

```yaml
provider: none
```

Phase-1 should work without any AI provider configured. In that case Jaime should still produce a non-AI diagnostic report from local state and logs.

## `model`

The model name to use with the configured provider.

Example:

```yaml
model: gemini-2.5-flash
```

For phase-1 this may be unused if `provider=none`.

## `api-token`

The API token for the configured AI provider. The recommended approach is to
store the token as a Juju secret so it is never exposed in `juju config` output
or operator logs.

**Recommended — Juju secret (production):**

```bash
# Store the token once
SECRET_URI=$(juju add-secret jaime-token token=<TOKEN>)

# Grant access to the application
juju grant-secret jaime-token jaime

# Set the config to the secret URI
juju config jaime api-token="${SECRET_URI}"
```

Jaime reads the `token` field from the secret content. The secret URI starts
with `secret:` and is safe to store in config.

**Development only — plain string:**

```bash
juju config jaime api-token="<TOKEN>"
```

Plain strings are accepted for convenience during local development, but the
token will be visible in `juju config jaime` output. Do not use this in
production.

The token is never written to Juju logs, JSONL audit logs, Markdown reports,
or AI prompts.

## `watch-statuses`

Comma-separated list of principal unit statuses that Jaime should monitor.

Default:

```yaml
watch-statuses: error,blocked
```

Recommended phase-1 values:

```yaml
watch-statuses: error,blocked
```

Later values may include:

```yaml
watch-statuses: error,blocked,waiting,maintenance,unknown
```

For phase-1, `waiting` and `maintenance` should usually be ignored because they can be normal during deployment, relation setup, upgrades, or restarts.

## `failure-timeout-minutes`

How long the principal unit must remain in a watched status before Jaime generates a report.

Default:

```yaml
failure-timeout-minutes: 5
```

Example behaviour:

1. Principal becomes `error`.
2. Jaime records an `incident_started` event.
3. If the principal recovers before the timeout, Jaime records `incident_recovered`.
4. If the principal is still unhealthy after the timeout, Jaime collects diagnostic context and generates a report.

## `cooldown-minutes`

Prevents duplicate reports for the same unresolved incident.

Default:

```yaml
cooldown-minutes: 30
```

Example:

If `update-status` runs every 5 minutes, Jaime should not call the AI provider or regenerate a full report every time while the same incident remains unresolved.

## `log-window-minutes`

How far back recent logs should be collected.

Default:

```yaml
log-window-minutes: 30
```

This should be used when collecting logs from sources such as:

- Juju unit logs
- systemd journal
- principal workload service logs
- local host diagnostics

## `max-context-lines`

Maximum number of lines to include in the compact context bundle.

Default:

```yaml
max-context-lines: 500
```

This protects against:

- excessive report size
- excessive AI provider cost
- context-window overflow
- leaking too much unrelated log data

## `report-dir`

Directory where report artifacts are written.

Default:

```yaml
report-dir: /var/log/jaime/reports
```

Reports should be written with predictable incident IDs, for example:

```text
/var/log/jaime/reports/incident-20260621-153000-postgresql-0.md
/var/log/jaime/reports/incident-20260621-153000-postgresql-0.context.json
```

## `audit-log-path`

Path to the structured JSONL audit log.

Default:

```yaml
audit-log-path: /var/log/jaime/events.jsonl
```

Each line should be one JSON object.

Example event:

```json
{"timestamp":"2026-06-21T15:30:00Z","event":"incident_started","principal_unit":"postgresql/0","status":"error","message":"principal unit entered watched status"}
```

## `watch-applications`

Comma-separated application names whose units on **this machine** should be
watched, in addition to the principal.

The machine charm always watches its related principal, whatever this option
says: relating the subordinate is the opt-in. An empty value therefore watches
the principal only, opens no controller connection and needs no credentials.

| Value | Watches |
|---|---|
| empty (default) | the principal only |
| `app1,app2` | the principal, plus any co-located units of those applications |
| `*` | the principal, plus every co-located unit |

Reach is bounded to units on the same machine, because the collectors read the
local host. A report about a unit elsewhere would carry this machine's disk,
memory, processes and firewall rules as evidence, so units on other machines
are never reported on.

A configured application with no unit on this machine is skipped silently. The
unit status names what is monitored, so absence from that list is the signal.

## `juju-api-user`

Name of a Juju user with `read` permission on this model. The controller API
checks workload status, but a unit's own agent identity does not have the
`ModelRead` permission that `Client.FullStatus` requires, so a dedicated user
is needed.

Required only when `watch-applications` is non-empty.

```bash
juju add-user jaime-observer
juju grant jaime-observer read <model-name>
```

When the value is empty and `watch-applications` is set, the charm reports a
blocked status. Credentials rejected by the controller are also blocked, while
a temporarily unreachable controller is reported as maintenance.

## `juju-api-password`

Password for `juju-api-user`. As with `api-token`, the recommended form is a
Juju secret.

```bash
SECRET_URI=$(juju add-secret jaime-juju-api password=<PASSWORD>)
juju grant-secret jaime-juju-api jaime
juju config jaime juju-api-password="${SECRET_URI}"
```

Jaime reads the `password` field from the secret content. A plain string is
accepted for local development but will be visible in `juju config` output. The
password is never written to logs, audit events, reports or AI prompts.

## `diagnostics`

Machine only. A JSON monitoring plan describing what to collect (log files,
processes, environment variables, network ports, systemd units, health
commands). When empty, Jaime attempts to generate a plan via AI on
relation-joined, and falls back to an empty plan if no provider is configured.

The schema lives in the charm's `diagnostics.py`.

## Phase-1 recommended config

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
