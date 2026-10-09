# The incident lifecycle

Jaime drives the lifecycle of one incident per monitored unit from Juju's
workload status. On every `update-status` it reads the unit's status and
decides whether to wait, report, cooldown, or recover.

```text
Jaime
-> identifies the units to monitor
-> checks unit status on every update-status
-> detects a watched status (error/blocked by default)
-> tracks how long the unit stays unhealthy
-> after failure-timeout-minutes, opens an incident
-> collects bounded context (logs, plan checks, host or pod evidence)
-> writes a Markdown report and a JSONL audit event
-> in suggest mode, sends the stored report to the provider and attaches the suggestion
-> respects cooldown-minutes before the next report
-> closes the incident on recovery
```

## The unhealthy timer

The timer is anchored to when Jaime first saw the unit go unhealthy, not to
Juju's `since` timestamp. A workload retrying in a loop re-sets its status on
every hook, so relying on Juju's value would reset the timer indefinitely and
never open an incident. Jaime therefore keeps its own `unhealthy_since` anchor
per unit.

## Episodes

A unit that flaps between two watched statuses - or re-sets the same status
with a new message - keeps a **single** incident and a single unhealthy timer.
The episode only ends when the unit leaves the watched statuses entirely.

## Recovery and cooldown

When a unit recovers, Jaime closes the incident and writes an
`incident-closed` audit event. The `cooldown-minutes` option prevents a new
report for the same unresolved incident from being generated too soon.

## Audit trail

Every lifecycle event is appended to `audit-log-path` (default
`/var/log/jaime/events.jsonl`): incident opened, report generated, suggestion
generated, and incident closed. The full history is queryable through
`list-incidents`.

`reset` closes every open incident, then **rotates** the audit log - the
closed history is archived to `events.jsonl.<timestamp>` and the configured
path restarts empty.
