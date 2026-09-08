# Jaime Architecture

## Overview

Jaime observes Juju units, detects sustained unhealthy states, collects compact diagnostics, and generates structured incident reports. It ships as two charm variants that share one codebase:

- **machine subordinate** (`charms/machine/`) — co-located with the principal on the same host. Reads the principal's workload status from local Juju hook tools (`goal-state`) and collects host diagnostics.
- **Kubernetes standalone** (`charms/k8s/`) — runs as its own pod and monitors other applications in the same Juju model. Reads workload statuses from the Juju controller API and collects pod logs, events, and metrics from the Kubernetes API.

Both variants are observe-first. AI is used to diagnose and to suggest, never to act: automatic remediation is not implemented, and `mode: act` is blocked until the Phase 7 safety controls exist.

Juju workload status is what opens an incident today. It is not a complete picture of workload health — see [Health model](#health-model).

## High-level architecture

### Machine subordinate

```text
Juju model
└── machine
    ├── principal charm unit
    │   └── application workload (daemon or systemd service)
    └── jaime subordinate unit
        ├── charm event handlers
        ├── principal status monitor (goal-state)
        ├── context collector (host + plan-driven)
        ├── incident tracker
        ├── JSONL audit logger
        ├── report generator
        └── AI provider adapter, optional
```

### Kubernetes standalone

```text
Juju model (one Kubernetes namespace)
├── watched application pods
│   └── workload containers
└── jaime-k8s pod
    ├── charm event handlers
    ├── workload status monitor ──► Juju controller API (Client.FullStatus)
    ├── context collector ────────► Kubernetes API (pods, logs, events, metrics)
    ├── incident tracker
    ├── JSONL audit logger
    ├── report generator
    └── AI provider adapter, optional
```

The k8s charm has no relation to the applications it watches; it is told what to monitor through the `watch-applications` config option.

## Code boundary

The architectural boundary is what matters and is expected to be stable:

```text
shared Jaime code    substrate-independent functionality: incident lifecycle,
                     reports, AI providers, suggestions, audit logging
charms/machine       machine-specific charm and host collector
charms/k8s           Kubernetes-specific charm, pod collector, Kubernetes API
```

The rule is that machine-specific host logic and Kubernetes-specific API logic stay out of the shared incident and business logic wherever practical. "Substrate-independent" means independent of *substrate*, not free of Ops: `core.py` is the shared charm controller layer and deliberately uses the Ops status classes. The genuinely substrate- and Juju-free modules are the ones below it — `incident.py`, `report.py`, `suggest.py`, `diagnostics.py`, `logutils.py`, and `providers/`.

Each concrete charm inherits from `CoreMixin` and `CharmBase`, creates a `StatusTracker`, observes its own events, and implements two substrate hooks:

```text
_collect_incident_context(unit_name, since_iso, incident) -> context dict
_collect_report_context(unit_name, since_iso)             -> context dict
```

### Current repository layout

The concrete layout below is *implementation and build detail*, not architecture. The directory name, the namespace-package trick, and the vendoring mechanism may all change without affecting the boundary above; a conventional layout such as `packages/jaime/` would be preferable if the repository is ever tidied up.

```text
jaime-package/jaime/        shared code
  core.py                   CoreMixin — modes, providers, secrets, actions, incident lifecycle
  incident.py               Incident, Suggestion, UsageMetadata
  principal.py              StatusTracker — persisted per-unit observations
  report.py                 Markdown report generation
  suggest.py                prompt building, response parsing, suggest/act engine
  diagnostics.py            diagnostics plan schema, validation, persistence
  controller.py             Juju controller API client (used by the k8s charm)
  logging.py, logutils.py   JSONL audit log, log filtering/de-duplication
  providers/                base, gemini, openrouter

charms/machine/src/
  charm.py                  machine-specific charm
  jaime/collector.py        host + plan-driven collection

charms/k8s/src/
  charm.py                  k8s-specific charm
  jaime/collector.py        pod collection
  jaime/k8s_api.py          Kubernetes API client

tests/unit/                 shared-library tests (imports only jaime-package)
tests/integration/          live-controller tests (never in the default test path)
charms/*/tests/             charm-specific tests only
```

Tests are split by what they exercise, not by which charm happens to host them.
`tests/unit/` sees only `jaime-package` on its `pythonpath`, which structurally
proves the shared library has no dependency on any charm-local module.
`CoreMixin` is tested once there against a dummy charm rather than duplicated
into both charm suites.

`jaime` is currently a **namespace package** — there is no `__init__.py` in either `jaime-package/jaime/` or `charms/*/src/jaime/`, so Python merges both directories into one `jaime` namespace at runtime. `from jaime.incident import Incident` resolves from the shared code and `from jaime.collector import collect_context` resolves from the charm-local module. At pack time the shared code is copied into the charm directory, because charmcraft's managed build container cannot follow a symlink out of the charm root.

## Technology Choices

### Juju Charm Framework

Both Jaime charms are implemented with the Ops Framework.

The Ops Framework is responsible for:

- charm lifecycle events
- relation handling
- action handling
- configuration management
- secret integration
- status updates

Business logic should remain outside the charm event handlers where possible to keep functionality testable without a running Juju model. `core.py` is the one deliberate exception: it is the shared charm controller layer and imports the Ops status classes.

### Dependencies

The machine charm needs only `ops`. The k8s charm additionally needs `websocket-client` for the Juju controller API. Both API clients (Juju controller and Kubernetes) are hand-written against the stdlib rather than pulling in `python-libjuju` or the Kubernetes client library, and neither charm shells out to `kubectl`.

## Core loop

Both charms poll on `update-status`. The machine charm inspects its related principal unit; the k8s charm inspects every unit of every application listed in `watch-applications`. Once a unit name and workload status have been obtained, the incident lifecycle in `CoreMixin._process_unit` is identical for both substrates.

Algorithm:

```text
on update-status:
  identify units to inspect
    machine: related principal unit via local charm context / goal-state
    k8s:     units of watch-applications via Juju controller API

  for each unit, read workload status

  if status is healthy:
    if there is an active incident:
      write incident-recovered event         # debug log only today
      close incident
    continue

  if status is in watch-statuses:
    if there is no active incident:
      create incident
      store first_seen timestamp
      write incident-start event

    if unhealthy duration is below failure-timeout-minutes:
      write still-unhealthy event            # debug log only today
      continue

    if report already generated for this incident:
      respect cooldown and continue

    collect context (substrate-specific, see below)
    generate Markdown report from the collected evidence
    write report-generated event

    if mode is suggest and an AI provider is configured:
      send the stored report to the provider
      attach the returned Suggestion and usage metadata to the incident
      write suggestion-generated event
```

The Markdown report is always generated from collected evidence alone. The
non-AI Markdown report containing that evidence is persisted before the
provider is called. The AI is then given the stored report as its input, and
its output is attached to the incident record rather than merged into the
report file. There is no separate raw context bundle: the report is the
persisted evidence artifact.

Context collection on the machine substrate:

```text
load diagnostics plan
if plan is available and has items in a section:
  collect per-plan context (tail log files, pgrep processes, systemctl is-active, ss port check, os.environ.get)
else for each empty or missing section:
  collect broad fallback (ps aux for processes, systemctl --failed for systemd, ss -tlnp for ports)
collect background context (Juju unit logs, disk usage, memory summary, socket statistics, firewall rules, charm config)
```

Context collection on the Kubernetes substrate:

```text
resolve the pod for the unit via the unit.juju.is/id annotation
collect per-container logs bounded by sinceTime and tailLines
collect Kubernetes events for the pod
collect CPU/memory usage from metrics-server, if present
summarise the pod (phase, node, conditions, containers, restarts, probes, resources, volumes)
collect the application's Juju config via the controller API
```

Collected log lines are filtered to error/warning lines with a surrounding
context window and then de-duplicated by normalised pattern, so repeated
failures collapse into one entry before anything is sent to an AI provider.

### Kubernetes monitoring cycle

Model-wide and controller-wide operations should happen **once per monitoring
cycle**, not once per watched unit:

```text
update-status
    ↓
open one Juju controller connection/login
    ↓
one Client.FullStatus
    ↓
filter watch-applications
    ↓
list/index Kubernetes pods once
    ↓
process watched units
    ↓
close controller connection
```

Two rules follow from this:

- Do not reconnect to the controller, re-authenticate, or re-list all pods for
  each watched unit. Fetch once, index by unit, then iterate.
- Do not cache a live controller WebSocket across hooks. Each hook is a
  separate process, so the connection can only be reused *within* one hook
  execution.

The current implementation meets the first half of this and not yet the second:
`_fetch_unit_statuses` performs a single connection, login, and `FullStatus`
per cycle, but `_fetch_app_config` opens a second connection and login for each
unit that opens an incident, and `get_pod_for_unit` lists every pod in the
namespace per unit (twice, when the annotation lookup misses and it falls back
to the name convention). With one or two watched applications the cost is
negligible; it grows linearly with the number of simultaneously unhealthy
units. Consolidating this is future work and is not required for correctness.

### Episodes and the unhealthy timer

Juju's `since` on a workload status means "when this status was last set", not
"when the unit became unhealthy". A charm retrying in a loop re-sets its status
on every hook — often alternating between two watched statuses, or re-setting
the same status with a new message — and each re-set bumps `since`.

Jaime therefore keeps its own anchor, `unhealthy_since`, in the state file:

- it is set from `since` when a unit **enters** a watched status
- it is held steady for as long as the unit stays in **any** watched status
- it is cleared when the unit leaves the watched statuses

An **episode** is a continuous run of watched (or unwatched) observations. The
increment, the open incident, and the cooldown timestamp reset only when the
unit crosses that boundary. Consequences:

- `failure-timeout-minutes` measures real, continuous unhealthiness, so a
  flapping workload eventually reports instead of resetting its timer forever
- flapping does not open a second incident, because the cooldown bookkeeping
  survives a `since` bump
- the log-collection window starts at `unhealthy_since`, so the causal error at
  the beginning of the failure is inside the collected window

Juju's raw `since` is still recorded, and is reported as `status_since` in the
incident events for traceability.


## Modes

### observe

Default mode.

Jaime may:

- read Juju/local state
- collect logs per diagnostics plan
- write JSONL audit logs
- write Markdown reports
- call the AI provider to generate the machine diagnostics plan

Jaime must not:

- restart services
- modify files outside its own state/report directories
- change charm or system configuration
- run AI-generated remediation commands
- call the AI provider for diagnosis; that starts at `suggest`

### suggest

Same as observe, but Jaime calls the AI provider to produce a diagnosis and a
single suggested remediation command, retrievable with the `get-suggestion`
action. No commands are executed. AI output is purely advisory.

The suggestion is attached to the incident and cached. It is regenerated only
when the operator supplies different `additional-context` or when the
configured model changes.

### act

**Not implemented, and not part of the current phase.** `run_act` exists in
`suggest.py`, but setting `mode: act` puts the charm in a blocked state.
Assisted remediation is Phase 7 of the roadmap.

When implemented, act mode must use:

- explicit operator intent
- a dry-run parameter, or an equivalent safety control
- strict command and policy allowlisting
- bounded execution
- a structured audit trail
- rollback metadata where practical

## Health model

Jaime distinguishes two health signals:

- **Juju health** — the workload status reported by Juju for a unit.
- **Workload health** — explicit application, host, pod, or diagnostic checks
  collected by Jaime itself.

Juju workload status is Jaime's primary lifecycle signal today. Diagnostic and
workload-health checks provide additional evidence and may in future
independently trigger an incident even when Juju still reports the unit as
`active`.

This distinction is not academic. Testing against AMS showed a workload that
was functionally broken while Juju continued to report the unit as `active`: a
charm only reports what its own hooks observed, so a workload that fails
between hooks, or fails in a way the charm does not check, stays invisible to
Juju.

The intended direction is therefore to widen the trigger from a single signal:

```text
Juju status  ->  incident  ->  diagnostics
```

to a composite health model:

```text
              ┌─ Juju workload status
              ├─ workload/application health checks
Health model ─┼─ machine/system health
              └─ Kubernetes pod/container health
                         ↓
                      incident
                         ↓
                     diagnostics
```

On the machine substrate the raw material for this already exists: the
diagnostics plan's health commands are collected on every incident. On
Kubernetes it would draw on readiness and liveness probe results, container
state, and restart counts. **No additional incident triggers are implemented
today** — this section records the direction so that nothing in the current
design forecloses it.

## Workload status source

The following is the source of the *Juju health* signal — currently the only
thing that opens an incident.

### Machine subordinate

Primary source:

- Juju charm context and hook tools
- subordinate relation context
- `goal-state`, filtered to the units actually related to this Jaime unit

The machine charm does not authenticate to the controller as an external
client.

### Kubernetes standalone

Primary source:

- the Juju controller API (`Client.FullStatus`), filtered by `watch-applications`

A unit's own agent identity lacks the `ModelRead` permission that
`Client.FullStatus` requires, so the operator must supply a dedicated Juju user
with `read` on the model via `juju-api-user` and `juju-api-password`. The
controller address, CA certificate, and model UUID are derived from the unit's
own `agent.conf`; only the credentials are configuration.

Authentication failure is reported as a blocked status. Transient controller
errors are reported as a maintenance status so the charm retries on the next
`update-status`.

### Workload-health evidence, both substrates

Collected as evidence for the report, and the raw material for a future
composite trigger:

- systemd state, journal logs, application logs, diagnostics-plan health commands (machine)
- pod phase, conditions, container restarts, probe definitions, Kubernetes events, metrics (k8s)

Today this evidence explains a failure that Juju already surfaced; it does not
yet decide on its own that a unit is unhealthy.

## AI usage

AI is optional. Every charm must produce a useful report with no provider
configured — the Markdown report is generated from collected evidence alone,
and AI never gates it.

AI is used for:

- generating the machine diagnostics plan on `principal-relation-joined`
- analysing the persisted incident evidence
- identifying likely root causes
- generating advisory remediation suggestions

AI is not used for:

- deciding whether a unit is unhealthy
- executing commands automatically
- receiving unlimited raw logs
- receiving secrets

### Providers

`providers/base.py` defines the provider contract; `gemini.py` and
`openrouter.py` implement it against their REST APIs using the stdlib only.
Provider connectivity is verified with `check()` on `config-changed` and again
before generation, so a bad token surfaces as a blocked status rather than a
failed incident.

The `api-token` and `juju-api-password` options accept a Juju secret URI
(`secret:<id>`, recommended) or a plain string. Tokens are never written to
logs, reports, or prompts.

### Usage accounting

Every LLM call records `UsageMetadata` — model, prompt/completion/total tokens,
and cost when the provider reports it. Entries are appended to a per-unit
`usage_log` in the state file, preserved across incident episodes, and never
cleared by a new episode. The `show-usage` action aggregates them into a global
summary with a per-model breakdown, or filters to a single incident.

## Data flow

```text
Juju workload status
  machine: goal-state          k8s: Client.FullStatus
        ↓
Status monitor (StatusTracker)
        ↓
Incident tracker
        ↓
Context collector
  machine ├── plan-driven (log files, processes, systemd units, network, env vars)
          └── background (Juju logs, disk, memory, sockets, firewall, charm config)
  k8s     ├── pod (logs per container, events, metrics, pod summary)
          └── Juju config via Application.Get
        ↓
Filter / de-duplicate / compact
        ↓
Report generator
        ↓
JSONL audit log + Markdown report
```

On the machine substrate a diagnostics plan feeds the collector. It is generated
once on `principal-relation-joined` — either from the `diagnostics` config option
or by asking the AI provider — and written to `/var/lib/jaime/diagnostics.json`.
`config-changed` does not regenerate it.

Optional:

```text
Stored report + operator-supplied additional-context
        ↓
AI provider adapter
        ↓
Suggestion (description + remediation command) + UsageMetadata
        ↓
Attached to the incident, returned by get-suggestion
```

AI output is never the only record. The non-AI Markdown report containing the
collected evidence is persisted before the provider is called, and the
suggestion references the incident it was generated from.

## Filesystem layout

Runtime paths, identical on both substrates:

```text
/var/lib/jaime/
  status-state.json           per-unit observations, unhealthy_since, incidents, usage log
  diagnostics.json            diagnostics plan (machine only)

/var/log/jaime/
  events.jsonl                append-only JSONL audit log
  reports/
    <incident-id>.md
```

`report-dir` and `audit-log-path` are configurable. On the machine substrate
these paths live on the host and survive hook and unit restarts. On Kubernetes
they live in the charm container's writable layer and are **lost when the pod is
replaced**, so incident history and usage accounting do not survive a
`juju refresh` or a node eviction.

## Audit events

Written to `events.jsonl`, one JSON object per line, each with a timestamp,
incident ID, and unit name:

```text
incident-start          incident opened
context-collected       context collected, with log line count (machine only)
report-generated        Markdown report written, with report path
suggestion-generated    AI suggestion attached, with usage metadata
act-command-executed    reserved for act mode, not reachable today
```

A second, more verbose set of lifecycle events is emitted as JSON to the Juju
debug log only, and is **not** part of the durable audit trail:

```text
principal-status-watched      unit entered a watched status
principal-status-cooldown     report suppressed by cooldown-minutes
principal-status-recovered    unit left the watched statuses
incident-opened               incident created, full incident record
incident-closed               incident closed by recovery or manual reset
```

Recovery is therefore visible in `juju debug-log` but does not currently append
a line to `events.jsonl`.

## Config

Shared by both charms:

```yaml
mode: observe                  # observe | suggest | act (act is blocked)
provider: none                 # none | gemini | openrouter
model: ""                      # defaults to gemini-2.5-flash / ~deepseek/deepseek-v4-flash-latest
api-token: ""                  # secret:<id> or plain string
watch-statuses: error,blocked
failure-timeout-minutes: 5
cooldown-minutes: 30
log-window-minutes: 30
max-context-lines: 500
report-dir: /var/log/jaime/reports
audit-log-path: /var/log/jaime/events.jsonl
```

Machine only:

```yaml
diagnostics: ""                # explicit diagnostics plan; empty means generate via AI
```

Kubernetes only:

```yaml
watch-applications: ""         # comma-separated app names; empty monitors nothing
juju-api-user: ""              # Juju user with read on the model
juju-api-password: ""          # secret:<id> or plain string
```

Monitoring is deliberately opt-in. An empty `watch-applications` means **monitor
nothing** and must never be interpreted as "all applications in the model" —
silently watching everything on install would be a surprising and expensive
default, and on a busy model it would generate reports and LLM calls the
operator never asked for. If model-wide monitoring is wanted later, it should be
requested explicitly with a distinct value such as `watch-applications: "*"`,
never inferred from an empty string.

## Juju actions

Machine subordinate:

```text
diagnose
collect-context
generate-report
get-suggestion [additional-context]
show-status
show-usage [incident-id]
reset
```

Kubernetes standalone:

```text
generate-report
get-suggestion [additional-context]
show-status
show-usage [incident-id]
reset
```

`diagnose` and `collect-context` are machine-only. All actions are read-only
with respect to the monitored workload.

Future actions:

```text
remediate
list-incidents
show-incident
clear-incident
```

## Acceptance tests

### Machine subordinate

1. Deploy a principal machine charm.
2. Deploy Jaime as a subordinate.
3. Configure the provider and mode **before** relating, since the diagnostics plan is generated on `principal-relation-joined` only.
4. Integrate Jaime with the principal.
5. Simulate or detect an unhealthy principal state.
6. Confirm Jaime records the incident start.
7. Wait for `failure-timeout-minutes`.
8. Confirm the report contains bounded Juju log excerpts, host checks, and plan-driven sections.
9. Confirm Jaime writes a Markdown report.
10. Confirm `get-suggestion` returns a diagnosis and a single command in `suggest` mode.
11. Confirm Jaime performs no remediation in observe mode.

### Kubernetes standalone

1. Deploy an application to a Kubernetes model.
2. Deploy Jaime as `jaime-k8s` (the shipped RoleBinding names that ServiceAccount).
3. Apply `jaime-k8s-rbac.yaml` in the model namespace.
4. Create a Juju user with `read` on the model and configure `juju-api-user`/`juju-api-password`.
5. Set `watch-applications` to the application under test.
6. Drive the application into a watched status.
7. Confirm Jaime records the incident start.
8. Confirm the report contains pod summary, Kubernetes events, and container logs — empty sections indicate missing Kubernetes RBAC.
9. Confirm `get-suggestion` returns a diagnosis and a single command in `suggest` mode.
10. Confirm Jaime performs no remediation in observe mode.

# Jaimie Roadmap

Phases 1 to 3 are implemented. Phases 4 to 7 are planned. The ideas below are unordered and not committed.

## Phase 1 – Machine Observe

Deploy Jaimie as a machine subordinate charm. Detect unhealthy principal units, collect diagnostics, and generate structured incident reports without modifying the environment.

## Phase 2 – AI-assisted Diagnosis

Integrate AI providers to analyse persisted incident evidence, identify likely root causes, and produce advisory remediation suggestions with a complete audit trail of every call. Nothing is executed.

## Phase 3 – Kubernetes / Multi-substrate Support

Support both a machine subordinate charm and a standalone Kubernetes charm from one codebase, with substrate-independent behaviour factored into shared code.

## Phase 3.5 – Test and tooling hygiene

Adopt the `tests/unit` and `tests/integration` split, move shared-library tests out of the charm suites, repair the root test entrypoints left broken by the Phase 3 restructure, and add lint. Done before CI so the workflow encodes the intended layout rather than the leftovers.

## Phase 4 – Improving user experience

Make both charms pleasant to build, deploy and read output from. Packaging that produces both artifacts without destroying either, Kubernetes deployment that tells the operator what it needs instead of failing silently, consistent configuration across both charms, richer incident reports, and diagnostics-plan parity so the Kubernetes charm collects to a plan as the machine charm already does.

Machine-charm controller access is deferred: it reverses a standing rule and needs a design note first.

## Phase 5 – CI/CD, integration tests and CharmHub release

Run all three unit suites and lint on every change, add integration tests that deploy the charms and drive a real fault through to a suggestion, then publish to CharmHub with tracks and channels. CI comes first in the implementation order, ahead of Phase 4, because it is cheap and guards everything after it.

## Phase 6 – Clustered operation for machine charms

Today a subordinate Jaime unit runs per principal unit, so a multi-unit application produces one independent incident, one LLM call and one report per unit, with no view of the cluster. Elect a leader that aggregates compacted context from its peers, makes a single LLM call per cluster incident, and owns the usage accounting. Then extend the machine charm to monitor a configured list of applications, as the Kubernetes charm does.

## Phase 7 – Assisted remediation

Execute operator-approved fixes. Requires command and policy allowlisting, bounded execution, a dry-run or equivalent safety control, a structured audit trail, and rollback metadata where practical. This is what `mode: act` will eventually enable; it is blocked today.

# Ideas on the roadmap

Unordered and not committed. Recorded so the direction is not lost.

- **Environment hygiene** — detect and safely clean residual resources left behind by charms, applications, or machines, with a focus on reclaiming failed or unprovisioned infrastructure.
- **Knowledge and support** — generate issue reports, identify known failure patterns, and assist operators with troubleshooting and bug filing.
- **Local knowledge engine** — learn from previously observed incidents, reports, and remediation outcomes to provide local recommendations without requiring an external AI provider.
- **Fleet and controller intelligence** — centralized visibility, controller integration, fleet-wide incident analysis, and optional user interfaces for managing multiple Jaimie deployments.
- **Composite health model** — let workload-health checks open incidents independently of Juju workload status, since a workload can be functionally broken while Juju still reports `active`. See [Health model](#health-model).
- **Kubernetes state durability** — persist incidents and usage across pod replacement via Juju unit state, which needs no volume, and consider Juju storage or a PVC for generated reports, which are too large for unit state.
- **External artifact sink** — forward the audit log and reports to Loki or object storage rather than relying on pod-local disk.
- **Kubernetes per-cycle consolidation** — one controller connection and one pod index per monitoring cycle rather than per watched unit. See [Kubernetes monitoring cycle](#kubernetes-monitoring-cycle).
- **Audit-log completeness** — write `still-unhealthy` and `incident-recovered` to `events.jsonl`, not only to the debug log.

