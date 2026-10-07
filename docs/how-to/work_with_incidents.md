# Work with incidents

Incidents are opened after a watched status persists past
`failure-timeout-minutes`, and closed when the unit recovers or on `reset`.
Most operator workflows are driven by actions:

```bash
juju run <application>/0 show-status           # monitoring state
juju run <application>/0 generate-report       # report for the open incident
juju run <application>/0 get-suggestion        # AI suggestion for the current incident
juju run <application>/0 list-incidents        # incident history from the audit log
juju run <application>/0 show-usage            # LLM usage (tokens, cost) per model
juju run <application>/0 reset                 # close all incidents, archive the audit log
```

## Inspect what is being watched

```bash
juju run <application>/0 show-status
```

lists every unit that is actually observed, including the workload, whether it
is in a watched status, and any open incident. Use it to confirm that a
configured application was found.

## Read incident history

```bash
juju run jaime/0 list-incidents
juju run jaime-k8s/0 list-incidents unit=postgresql-k8s/0
```

Each record correlates the incident's start, report and closure, and includes
the workload status message captured when it opened. Incidents without a
closure row are reported as open. See
[Actions](../reference/actions.md) for the full reference.

## Ask for a diagnosis

`get-suggestion` accepts optional `additional-context`, treated as
authoritative for the diagnosis:

```bash
juju run jaime/0 get-suggestion \
  additional-context="Disk was resized 20 min ago; pgdata is on /dev/sdb1"
```

The suggestion is cached on the incident and regenerated only when that
context or the model changes.

## Reset

`reset` closes every open incident, then **rotates** the audit log: the closed
history is archived to `events.jsonl.<timestamp>` and the configured path
restarts empty. `list-incidents` afterwards returns nothing while the archive
is retained on disk for forensics.

## Troubleshooting

- A configured application with no unit in reach is skipped silently — run
  `show-status` to see what is actually observed.
- A blocked Kubernetes charm usually means missing or rejected `juju-api`
  credentials, missing Kubernetes RBAC, or a `watch-applications` name that is
  not on the model.
- An empty machine diagnostics plan means the provider was configured after
  relating; remove and re-add the relation.
- `generate-report` and `get-suggestion` fail when no incident is open.
