# Actions

This document describes the Juju actions exposed by **Jaime — Juju AI Medic Engine**.
It is a reference to the most recently added actions; `docs/config.md` and the
charm `actions.yaml` files remain the full source of truth for every action.

For phase-1, Jaime exposed only one action, `diagnose`; both charms have since
gained the incident lifecycle and AI-suggestion actions. All actions are
read-only with respect to the monitored workload.

## `diagnose`

Collects and prints basic information about the related principal charm.

The initial MVP action should return JSON containing:

- principal application name
- principal unit name
- principal unit status
- principal charm version, if discoverable
- Jaime unit name
- timestamp

It should not:

- call an external AI provider
- remediate anything
- restart services
- modify files
- upload logs
- require provider credentials

## Example

```bash
juju run jaime/0 diagnose
```

Expected output shape:

```json
{
  "principal": {
    "application": "postgresql",
    "unit": "postgresql/0",
    "status": "active",
    "status_message": "",
    "charm_version": "14/stable or unknown"
  },
  "jaime": {
    "unit": "jaime/0",
    "mode": "observe"
  },
  "timestamp": "2026-06-21T15:30:00Z"
}
```

The exact `charm_version` value depends on what the charm can safely discover from local Juju context or hook tools. If it cannot be discovered reliably in phase-1, the action should return:

```json
"charm_version": "unknown"
```

## Behaviour

The action should:

1. Identify the related principal unit.
2. Read the principal unit state from local Juju context or hook tools where possible.
3. Attempt to determine the principal charm version.
4. Return a JSON object.
5. Write a structured JSONL audit event indicating that `diagnose` was run.

## Error handling

If the principal unit cannot be determined, the action should fail clearly.

Example output:

```json
{
  "error": "principal_unit_not_found",
  "message": "Jaime is not related to a principal unit or could not determine the principal from local context."
}
```

If the charm version cannot be determined, the action should not fail. It should return:

```json
"charm_version": "unknown"
```

## Acceptance criteria

The `diagnose` action is complete when:

- `juju run jaime/0 diagnose` returns valid JSON
- the JSON includes principal unit name and status
- the JSON includes principal charm version or `unknown`
- no host mutation occurs
- no AI provider is required
- one JSONL audit event is written for the action

## `list-incidents`

Lists incidents recorded in the audit log (`events.jsonl`), newest first. Each
entry is correlated from the incident's lifecycle events by incident id:
`incident-start` (opened_at, workload, first-seen), `report-generated`
(report_path), and `incident-closed` (closed_at). Incidents without a closure
row — including all open incidents — are reported with `status: "open"`.

```bash
juju run jaime/0 list-incidents              # all incidents
juju run jaime-k8s/0 list-incidents unit=postgresql-k8s/0
```

Optional parameters:

- `unit` — restrict the result to incidents for one unit (e.g. `postgresql/0`).

The result is a JSON array under `result`, with fields `incident_id`, `unit`,
`workload`, `status_message`, `first_seen`, `status_since`, `opened_at`,
`report_path`, `closed_at`, `status`. `status_message` is the workload's
reason at the time the incident opened (empty for incidents logged before this
field existed). A missing or malformed audit log is tolerated and
yields an empty list rather than an error.

A `reset` closes every open incident and then rotates the audit log: the
closed history is archived to `events.jsonl.<timestamp>` and the configured
path restarts empty, so this action returns an empty list afterwards while the
archived trail is kept on disk.
