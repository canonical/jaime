# Actions

This reference describes the Juju actions exposed by **Jaime — Juju AI Medic
Engine**. The charm `actions.yaml` files remain the source of truth. All actions
are read-only with respect to the monitored workload.

The machine subordinate charm (`jaime`) exposes every action below; the
Kubernetes standalone charm (`jaime-k8s`) exposes all except `diagnose` and
`collect-context`, and adds `show-setup-steps`.

## `show-status`

Show the current monitoring state for every monitored unit: workload, the active
incident if any, and the last observation. Returns a JSON array under `result`
with fields `unit`, `workload`, `status-message`, `first-seen`, `status-since`,
`increment`, `last-reported`, `incident-id`, `incident-opened-at`.

```bash
juju run jaime/0 show-status
```

## `show-usage`

Show AI token usage and cost: a global summary across all incidents, or
per-incident detail when `incident-id` is given.

| Parameter | Type | Description |
|---|---|---|
| `incident-id` | string | Incident id to filter by. Empty returns the global summary. |

```bash
juju run jaime/0 show-usage
juju run jaime/0 show-usage incident-id=<incident-id>
```

## `get-suggestion`

Return the AI suggestion for the current open incident: a root-cause
description and a single suggested command. The command is never executed.

| Parameter | Type | Description |
|---|---|---|
| `additional-context` | string | Extra context to include in the AI prompt. |

```bash
juju run jaime/0 get-suggestion \
  additional-context="Disk was resized 20 min ago; pgdata is on /dev/sdb1"
```

Results: `incident-id`, `description`, `commands`, `command-count`,
`generated-at`, `cached`, and, when the provider reports usage, `model`,
`prompt-tokens`, `completion-tokens`, `total-tokens` and `cost-usd`. The
suggestion is cached on the incident and regenerated only when the
`additional-context` or the configured model changes, so `cached` reports
whether the provider was called. The action fails when no incident is open, when
the mode is `observe`, or when no suggestion is available.

## `generate-report`

Generate and return a Markdown report for the current open incident. The file is
written to `report-dir` as `<incident-id>.md`.

```bash
juju run jaime/0 generate-report
```

Results: `incident-id` and `report-path`. The action fails when no incident is
open.

## `list-incidents`

List incidents recorded in the audit log, newest first. Each entry correlates
the incident's `incident-start`, `report-generated` and `incident-closed`
events by incident id, including open incidents, and reports the workload status
message captured when the incident opened.

| Parameter | Type | Description |
|---|---|---|
| `unit` | string | Restrict the result to incidents for one unit, for example `postgresql/0`. Empty returns all incidents. |

```bash
juju run jaime/0 list-incidents
juju run jaime-k8s/0 list-incidents unit=postgresql-k8s/0
```

Returns a JSON array under `result` with fields `incident_id`, `unit`,
`workload`, `status_message`, `first_seen`, `status_since`, `opened_at`,
`report_path`, `closed_at` and `status` (`open` or `closed`). A missing or
malformed audit log yields an empty list rather than an error.

## `reset`

Close every tracked incident and clear the monitoring state.

```bash
juju run jaime/0 reset
```

`reset` closes open incidents, then rotates the audit log: the closed history is
archived to `events.jsonl.<timestamp>` and the configured path restarts empty,
so `list-incidents` returns nothing afterwards while the archive is retained on
disk.

## `diagnose` (machine charm)

Collect basic information about the related principal charm. Read-only.

```bash
juju run jaime/0 diagnose
```

Results: `principal-unit` (or `unknown` when none is related), `jaime-unit`,
`jaime-mode` and `timestamp`.

## `collect-context` (machine charm)

Collect a bounded context bundle for the related principal, write it to disk,
and return its path.

```bash
juju run jaime/0 collect-context
```

Results: `unit` and `context-path`. The action fails when no principal unit is
related.

## `show-setup-steps` (Kubernetes charm)

Print the exact setup steps for the Kubernetes charm: Kubernetes RBAC, the Juju
observer user, secrets, and configuration, with the model and application names
pre-filled. Read-only.

```bash
juju run jaime-k8s/0 show-setup-steps
```
