## Requirements

### Requirement: A multi-unit fault produces one cluster incident

When several peer units watching the same monitored application open a
watched-status incident within the same aggregation cycle, the leader SHALL
correlate them into one cluster incident rather than leaving them independent.

#### Scenario: two peers fault together
- **WHEN** two peer units of the same monitored application both have an open,
  watched-status incident
- **THEN** exactly one cluster incident exists for that application
- **AND** exactly one LLM call is made for it, owned by the leader
- **AND** exactly one report reflects both units' context

### Requirement: A stale peer does not block aggregation

The leader SHALL aggregate using the most recent data available for each peer
and SHALL NOT wait for a peer that has not published recently.

#### Scenario: a peer stops publishing
- **WHEN** a peer has not published to the relation in more than 2× its own
  `failure-timeout-minutes`
- **THEN** the leader still aggregates and produces a report
- **AND** that peer's contribution is marked stale, with its last-known
  timestamp, rather than silently omitted

### Requirement: A cluster incident survives a leader change

#### Scenario: leader changes mid-incident
- **WHEN** Juju elects a new application leader while a cluster incident is
  open
- **THEN** the new leader continues the same cluster incident id, read from
  the peer application databag
- **AND** no new incident is opened and no existing one is duplicated

### Requirement: Two co-located Jaime units never double-report one unit

#### Scenario: same machine, same watched unit
- **WHEN** two Jaime units share a machine and both resolve the same
  co-located unit as monitored
- **THEN** only the unit with the lexicographically lower name opens and
  reports the incident for that unit
- **AND** the other unit's status says it is standing down for that unit,
  rather than silently doing nothing

### Requirement: A follower never answers for a clustered incident on its own

#### Scenario: `ask` on a follower
- **WHEN** `ask`, `get-suggestion` or `list-suggestions` is run on a follower
  unit for an incident that is cluster-aggregated
- **THEN** the action fails
- **AND** the failure message names the current leader unit
- **AND** no LLM call is made by the follower

#### Scenario: `ask` on a follower for a non-clustered incident
- **WHEN** the same actions are run on a unit for an incident that has no
  cluster aggregation (e.g. a single-unit application)
- **THEN** behaviour is unchanged from today

### Requirement: Usage accounting for a cluster incident is not double-counted

#### Scenario: leader makes the cluster incident's LLM call
- **WHEN** the leader calls the provider for a cluster incident
- **THEN** exactly one `usage_log` entry is recorded for that call
- **AND** `show-usage` for that incident id reflects one call, not one per peer
