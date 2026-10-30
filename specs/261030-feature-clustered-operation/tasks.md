# Tasks: Clustered operation for machine charms

## 1. Agree the design
- [ ] [project] Review and agree `proposal.md`, `design.md` and
      `requirements.md` — in particular Decisions 3, 4 and 5, which are new
      calls this spec makes rather than restating `ARCHITECTURE.md`
- [ ] [project] Confirm `TASKS.md` 6.1 (prompt budget and compaction) lands
      first, or at minimum its `build_projection()` primitive, since peer
      publication depends on it

## 2. Implement: peer relation and publication
- [ ] [charm] Add a `peers:` relation to `charms/machine/charmcraft.yaml`
- [ ] [charm] Each unit publishes its own compacted local context (via 6.1's
      projection) and current incident state to the peer relation databag on
      every monitoring cycle
- [ ] [python] Bound the published payload to a fixed size; log and truncate
      rather than fail if a peer's projection still exceeds it
- [ ] [charm] Same-machine dedup (Decision 5): detect two Jaime units sharing a
      machine and watching the same co-located unit; the lexically lower unit
      name acts, the other stands down and reports this in its unit status,
      replacing 4.3's detect-and-report-only behaviour

## 3. Implement: leader election and aggregation window
- [ ] [charm] Elect a leader via Juju's `is_leader()`, restricted to peers
      currently watching the same monitored application as the incident being
      aggregated (Decision 2's foundation; see the "Open implementation check"
      in `design.md` for the known gap when the Juju leader isn't watching it)
- [ ] [python] Aggregate from the peer relation databag's current state at
      trigger time; no explicit wait (Decision 3)
- [ ] [python] Mark a peer stale when its last publication is older than 2× its
      own `failure-timeout-minutes`; never block on it

## 4. Implement: cluster incident lifecycle
- [ ] [python] A cluster incident model, keyed by monitored application,
      distinct from the existing per-unit incident; correlates the per-unit
      incidents opened by each peer
- [ ] [charm] Store cluster incident state in the peer application databag (not
      a single unit's local state), so it survives leader re-election

## 5. Implement: leader-owned LLM call, actions and usage accounting
- [ ] [python] One LLM call per cluster incident, made only by the leader
- [ ] [charm] `ask`, `get-suggestion` and `list-suggestions` reject on a
      follower for a clustered incident, naming the current leader unit
      (Decision 4); unaffected for a non-clustered incident
- [ ] [python] Leader-owned `usage_log` entry per cluster-incident LLM call; a
      follower never writes its own entry for a clustered call
- [ ] [charm] `show-usage` reports cluster-level totals for a clustered
      incident, not one per unit

## 6. Test
- [ ] [test] Two peers, same application, same fault: exactly one cluster
      incident, one report, one LLM call
- [ ] [test] A peer that never reports does not block the leader; its
      contribution is marked stale with a timestamp
- [ ] [test] Leader changes mid-incident: the cluster incident continues
      without reopening or duplicating
- [ ] [test] Two peers, same machine, same watched unit: exactly one acts
      (lexically lower name), the other's status says it stood down
- [ ] [test] `ask`/`get-suggestion`/`list-suggestions` on a follower for a
      clustered incident fail and name the leader; unaffected for a
      non-clustered incident
- [ ] [test] No double-counted usage across a leader change or across peers

## 7. Document and close
- [ ] [docs] Document clustered mode in `docs/reference/` once behaviour lands
- [ ] [project] Remove the "detect and report" dedup language from
      `ARCHITECTURE.md`'s "Multiple Jaime units on one machine" once this spec
      ships, and record the design (aggregation key, leader scope, dedup rule,
      follower-action behaviour) in `ARCHITECTURE.md`'s Phase 7 section
- [ ] [project] On merge: set `Status: done` in `proposal.md` and update the
      descriptive sections of `ARCHITECTURE.md`, `TASKS.md` 7 and
      `CHANGELOG.md`
