# Design: Clustered operation for machine charms

Decisions 3, 4 and 5 are new design calls this spec makes — flagged `(proposed)`
below so they're easy to find and override during review. Decisions 1 and 2
restate what `ARCHITECTURE.md` Phase 7 already settled, so the rest of the
design has a stated foundation to build on.

## Decision 1: aggregation key is the monitored application, not the peer set

Already decided in `ARCHITECTURE.md` Phase 7. All units of the Jaime
subordinate share one peer relation, including units related to different
principals — a Jaime related to both `postgresql` and `redis` has both as
peers. Restated because every later decision in this spec depends on it.

Rejected:

- **Aggregate the raw peer set.** Merges unrelated workloads (`postgresql` and
  `redis`) into one incident whenever they happen to share Jaime peers.

## Decision 2: continuous one-way publication, not request/response

Each unit publishes its compacted context and incident state to its own side
of the peer relation on every monitoring cycle, unconditionally — it does not
wait to be asked, and the leader does not request data from a specific peer.
The leader aggregates by reading whatever is currently in the databag when it
needs to.

Rejected:

- **Request/response over the peer relation.** Juju relation data is not a
  synchronous channel: a "request" would need a separate hook round-trip
  (follower sets a request key, leader's `relation-changed` fires, leader
  writes a response key, follower's `relation-changed` fires again), adding
  multi-hook latency and failure modes for no benefit over always-on
  publication.

This decision is why Decision 3 (below) does not need a request/response
protocol either — the leader already has the most recent thing each peer
published, continuously, without asking.

## Decision 3 (proposed): no explicit aggregation-window wait

The leader aggregates from whatever is currently in the peer relation databags
at the moment it needs to act for a cluster incident — it does not wait for a
fixed window or a missed-cycle count before proceeding. A peer that has not
published recently is marked explicitly as stale (using that peer's own
last-published timestamp) in the aggregated context and the report, rather
than silently omitted or used to block the leader.

"Recently" is the peer's own `failure-timeout-minutes` (already configured,
already per-unit) multiplied by a small constant (proposed: 2×), used only to
decide the stale/fresh label — never to block aggregation.

Rejected:

- **A fixed wait before aggregating.** `update-status` cadences are
  independent per unit and not controlled by Jaime, so any fixed wait is
  either too short for a legitimately slow peer or needlessly delays the
  common case where every peer already published this cycle.
- **A missed-cycle counter per peer.** Requires the leader to track cycle
  counts per peer across restarts and leader changes — state for a problem
  Decision 2's continuous publication already avoids: the leader never needs
  to know how many cycles a peer missed, only how old its last contribution is.

## Decision 4 (proposed): a follower rejects, it does not proxy or answer alone

`ask`, `get-suggestion` and `list-suggestions`, run on a follower for a
clustered incident, fail with a message naming the current leader unit. The
operator (or a chat bot, per 4.15) re-runs the action against that unit.

Rejected:

- **Follower proxies to the leader and returns its answer.** Needs the same
  synchronous request/response Decision 2 rejected, across unit boundaries
  rather than within one relation; Juju actions do not give charm code a way
  to block a running action on another unit's next hook.
- **Follower answers independently from its own local context.** Produces a
  second LLM call and a second, possibly inconsistent, answer for the same
  cluster incident — exactly the duplicated cost and duplicated view this
  phase exists to remove.

## Decision 5 (proposed): same-machine dedup by lexical unit-name order

When two Jaime units share a machine and both resolve the same co-located unit
as monitored (the 4.3 "detect and report" case), the unit with the
lexicographically lower name acts (opens the incident, collects, reports); the
other stands down and says so in its own unit status. Both units compute this
independently from data each already has in the peer relation (their own unit
names, and the co-located-unit resolution each already performs for 4.3) — no
election round-trip is needed.

Rejected:

- **An explicit election protocol (claim/ack over the relation).** Adds a
  multi-hook handshake for a choice that is already fully determined by data
  both sides already have.
- **Lowest machine-wide leader always wins.** Conflates this phase's two
  distinct dedup problems — "two Jaime units, same machine, same watched unit"
  (this decision) and "which peer is the Jaime application's Juju leader"
  (Decision 2's leader, used for a different purpose: the single LLM call) —
  which do not have to be, and in general are not, the same unit.

## Bounds and safety

- Published peer context is bounded by the 6.1 projection's budget, not the
  full report; this spec adds no new unbounded data to a relation databag.
- Stale-peer labelling is informational only — it never blocks the leader
  from producing a report and a suggestion with the data it has.
- No new network exposure: everything here uses the existing peer relation
  (Juju-managed transport), consistent with the rejected-alternatives list in
  `ARCHITECTURE.md`'s "Conversational suggestions" section, which already
  turned down an in-charm HTTP listener for a related reason.
- Leader-owned usage accounting means a follower never calls a provider for a
  clustered incident (Decision 4), so there is no path to double-billing one
  cluster incident across peers.

## Edge cases

| Case | Behaviour |
|---|---|
| Juju-elected application leader is not watching the relevant application this cycle | No leader acts for that incident this cycle; documented as a known gap, not solved here — see "Open implementation check" |
| A peer never publishes (down, cordoned, crashed hook) | Marked stale using its own `failure-timeout-minutes` × 2; aggregation proceeds without it |
| Leader changes mid-incident | New leader reads the same cluster incident id from the peer application databag and continues it; no reopen, no duplicate |
| Two co-located Jaime units, same watched unit | Lexically lower unit name acts; the other's status says so |
| `ask` on a follower for a clustered incident | Action fails, names the current leader unit; no LLM call |
| `ask` on a follower for a *non*-clustered (single-unit) incident | Unaffected — behaves exactly as it does today |
| All peers of an application are followers (leader not watching it) | Same as the first row: no aggregation this cycle for that application |

## Open implementation check

Juju's `is_leader()` is scoped to the whole application, not to which
principal a given unit happens to be watching. Decision 3's leader is
"restricted to peers watching the same monitored application," which can be
empty of the Juju-elected leader (first edge case above). This spec does not
add a secondary, per-application election to cover that gap — it accepts the
gap and documents it, consistent with "Simplicity First" in `AGENTS.md`. If
this proves too lossy in practice (the Juju leader frequently isn't watching
the relevant application), a secondary per-application election is the
follow-up, not a reason to block this spec.

## Non-goals

- Cross-model or cross-application federation (jaime-microceph talking to
  jaime-k8s talking to jaime-loki). See the proposal's "Not changing" section
  and `ARCHITECTURE.md`'s "Fleet and controller intelligence" idea.
- Any change to `mode: act` or command execution.
- A UI or external aggregator for cluster incidents — `show-status`,
  `list-incidents`, `ask` and `list-suggestions` are extended to understand a
  cluster incident, not replaced.
