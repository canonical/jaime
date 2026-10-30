# Proposal: Clustered operation for machine charms

Type: feature
Status: proposed

Tracks `TASKS.md` 7. Depends on `TASKS.md` 6.1 (the compacted-context
projection peers publish).

## Why

A subordinate Jaime unit runs per principal unit today, so a multi-unit
application (a 3-unit `postgresql`, say) produces as many independent
incidents, LLM calls and reports as it has units, for what is often one
cluster-wide fault. There is no view that ties them together.

Two Jaime units can also already land on the same machine, when Jaime is
related to more than one principal on that host. With `watch-applications`
set, both then resolve the same co-located unit as monitored and open
duplicate incidents for the same fault. 4.3 detects this and reports it in the
unit's status, but does not fix it — `ARCHITECTURE.md`'s "Multiple Jaime units
on one machine" explicitly defers the fix to the peer relation this spec adds.

## What changes

- A peer relation on the machine subordinate charm (`charms/machine/charmcraft.yaml`).
- Every unit continuously publishes its own compacted local context (the 6.1
  projection, not the raw report) and its current incident state to the peer
  relation databag, on every monitoring cycle.
- A leader — restricted to the peers currently watching the same monitored
  application as the incident being aggregated, not the whole peer set —
  aggregates that published state into one cluster incident, keyed by the
  monitored application.
- The leader makes the single LLM call for a cluster incident and owns usage
  accounting for it.
- `ask`, `get-suggestion` and `list-suggestions`, run on a follower for a
  clustered incident, fail and name the current leader unit, rather than
  answering locally or silently proxying.
- Two Jaime units sharing a machine and watching the same co-located unit stop
  opening duplicate incidents: a deterministic rule picks exactly one to act,
  the other stands down and says so in its status.

## Not changing

- Per-unit detection and the existing single-unit incident lifecycle
  (`principal.py` / `StatusTracker`). This phase aggregates on top of it; it
  does not replace it, and a unit still tracks and reports its own incident
  independently of whether aggregation succeeds.
- Multi-application monitoring (4.3), already shipped and bounded to a unit's
  own host.
- Cross-model or cross-application aggregation — for example a machine Jaime
  and a Kubernetes Jaime correlating one incident together. Explicitly out of
  scope; tracked separately under the "Fleet and controller intelligence" idea
  in `ARCHITECTURE.md`. No HTTP endpoint is added to either charm by this spec.
- The Kubernetes charm, which has no peer relation and is unaffected.
- `mode: act`. Still blocked; aggregation changes nothing about what gets
  executed.

## Impact

- `charms/machine/charmcraft.yaml` — new `peers:` relation
- `charms/machine/src/charm.py` — peer data publication, leader election
  scoped to the watched application, cluster incident wiring
- `jaime-package/jaime/incident.py` (or a new module) — cluster incident model
- `jaime-package/jaime/core.py` / `jaime-package/jaime/actions.py` — follower
  rejection for clustered-incident actions, leader-owned usage accounting
- Depends on `jaime-package/jaime/projection.py` from 6.1 for the published
  per-peer context
- `tests/unit/`, `charms/machine/tests/`
- `ARCHITECTURE.md` descriptive sections and `docs/reference/` — updated on
  close, not as part of this proposal
