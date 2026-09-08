# Jaime Tasks

Phases 0 to 3.5 are implemented. Phases 4 to 6 are the active plan.

Later phases and unscoped ideas live in `ARCHITECTURE.md`, which is the roadmap
source of truth. They are deliberately not listed here until they are worth
breaking into tasks.

Ordering note: 5.1 (CI) runs ahead of Phase 4 because it is cheap and guards
every change after it. 5.2 depends on 4.1, since integration tests cannot
deploy reliably until packaging stops destroying artifacts. 5.3 depends on
4.2, 4.4 and 4.6.

## 0. Phase 0 — Repository bootstrap

- [x] Create charm repository structure
- [x] Add README.md
- [x] Add AGENTS.md
- [x] Add architecture.md
- [x] Add tasks.md
- [x] Add .gitignore
- [x] Add initial license decision

## 1. Phase 1 — Machine Observe

Deploy Jaimie as a machine subordinate charm. Detect unhealthy principal units, collect diagnostics, and generate structured incident reports without modifying the environment.
Only logic for `observe` mode is added at this phase.

### 1.1. Charm skeleton

- [x] [charm] Add `charmcraft.yaml`
- ~~[ ] [charm] Add `metadata.yaml` for machine subordinate charm~~
- [x] [charm] Add `config.yaml`
- [x] [charm] Add `actions.yaml`
- [x] [charm] Add `src/charm.py`
- [x] [charm] Add basic charm unit status handling
- [x] [charm] Add relation handling for principal/subordinate relation

### 1.2. Config

- [x] [charm] Add `mode`, default `observe`
- [x] [charm] Add `watch-statuses`, default `error,blocked`
- [x] [charm] Add `failure-timeout-minutes`, default `5`
- [x] [charm] Add `cooldown-minutes`, default `30`
- [x] [charm] Add `log-window-minutes`, default `30`
- [x] [charm] Add `max-context-lines`, default `500`
- [x] [charm] Add `provider`, default `none` or `gemini`
- [x] [charm] Add `model`
- [x] [charm] Add token/secret config only after non-AI observe path works

### 1.3. Diagnostics plan generation

- [x] [charm] Add `diagnostics` config variable to `config.yaml`
- [x] [python] Define diagnostics JSON schema (`src/jaime/diagnostics.py`)
- [x] [python] Add diagnostics validation against schema
- [x] [python] Add AI prompt builder for diagnostics generation
- [x] [python] Add diagnostics file persistence/reading (`/var/lib/jaime/diagnostics.json`)
- [x] [python] Add Gemini provider using REST API (`src/jaime/providers/gemini.py`)
- [x] [charm] On `principal-relation-joined`: if `diagnostics` config empty, generate via AI and write to file
- [x] [charm] On `principal-relation-joined`: if `diagnostics` config non-empty, validate and write to file
- [x] [charm] Create `src/jaime/` and `src/jaime/providers/` package structure

### 1.4. Principal status monitoring

- [x] [charm] Implement principal unit discovery via goal-state
- [x] [python] Implement `StatusTracker` for per-unit state persistence
- [x] [python] Implement real status reader using Juju goal-state hook tool
- [x] [charm] Detect watched statuses from config
- [x] [python] Detect recovery (status leaving watch list)
- [x] [python] Avoid duplicate incident creation via incident ID tracking

### 1.5. Incident tracking

- [x] [python] Define `Incident` dataclass with open/close lifecycle
- [x] [python] Use UUID4 for incident IDs
- [x] [python] Store active incident state in `StatusTracker` under `/var/lib/jaime/status-state.json`
- [x] [python] Record `first_seen` timestamp per unit
- [x] [python] Record current status per unit
- [x] [python] Record report generation state as `last_reported` timestamp
- [x] [python] Close incident on recovery detection
- [x] [python] Add cooldown logic to prevent repeated report generation

### 1.6. Context collection

- [x] [charm] Collect Juju goal-state/status context via hook tool
- [x] [python] Collect recent Juju unit logs bounded by `log-window-minutes` and `max-context-lines`
- [x] [python] Collect systemd failed units via `systemctl --failed`
- [x] [python] Collect disk usage via `df -h`
- [x] [python] Collect memory summary via `free -h`
- [x] [python] Enforce `max-context-lines` on all log/collect output
- [ ] [python] Collect journal snippets bounded by time/lines
- [ ] [python] Redact obvious secrets/tokens/passwords

### 1.6a. Plan-driven context collection

- [x] [python] Load diagnostics plan in collector and iterate sections
- [x] [python] Collect per-plan log files (`tail -n max_lines` each path)
- [x] [python] Collect per-plan processes (`pgrep -f` and compare count)
- [x] [python] Collect per-plan systemd units (`systemctl is-active`)
- [x] [python] Collect per-plan network ports (`ss -tlnp` filtering)
- [x] [python] Collect per-plan environment variables (`os.environ.get`)
- [x] [python] Return plan_results alongside background context
- [x] [python] Handle empty plan gracefully (broad fallback)
- [x] [python] Broad fallback for missing plan (`ps aux`, `ss -tlnp`, `systemctl --failed`)

### 1.7. Structured logging

- [x] [python] Create append-only JSONL audit logger (`logging.py`)
- [x] [python] Write `incident-start` event on incident open
- [x] [python] Write `context-collected` event with log line count
- [x] [python] Write `report-generated` event with report path
- [x] [python] Emit lifecycle events to the Juju debug log (`incident-opened`, `incident-closed`, `principal-status-watched`, `principal-status-cooldown`, `principal-status-recovered`)
- [ ] [python] Write `still-unhealthy` event to the audit log during timeout wait (debug log only today)
- [ ] [python] Write `incident-recovered` event to the audit log on recovery (debug log only today)
- [x] [python] Include timestamp, incident ID, principal unit, status in all events

### 1.8. Reports

- [x] [python] Generate non-AI Markdown report from collected context
- [x] [python] Include timeline (incident ID, first seen, generated at)
- [x] [python] Include observed workload status
- [x] [python] Include bounded Juju unit log excerpts
- [x] [python] Include host checks (disk, memory, systemd)
- [x] [python] Include plan-driven sections (log files, processes, systemd units, network ports, env vars)
- [ ] [python] Include suggested manual next steps
- [x] [python] Store reports under `/var/log/jaime/reports/`

### 1.9. Actions

- [x] [charm] `diagnose`: return current principal context and mode
- [x] [charm] `collect-context`: collect and return context
- [x] [charm] `generate-report`: collect context, generate report, return path and content
- [x] [charm] `show-status`: return current monitoring state for all units
- [x] [charm] `reset`: close open incidents and clear status state
- [x] [charm] Ensure all actions are read-only in observe mode

### 1.10. Tests

- [x] [test] Set up pytest with test infrastructure (`pyproject.toml`, venv, tests/ structure)
- [x] [test] Unit test `diagnostics.py` — `validate_diagnostics` (valid/invalid plans, schema edge cases)
- [x] [test] Unit test `diagnostics.py` — `build_prompt`, `make_empty_plan`
- [x] [test] Unit test `diagnostics.py` — `write_diagnostics_file`, `read_diagnostics_file` (round-trip, missing file, invalid JSON, directory creation)
- [x] [test] Unit test `diagnostics.py` — `DIAGNOSTICS_SCHEMA` structure
- [x] [test] Unit test `providers/base.py` — abstract class contract
- [x] [test] Unit test `providers/gemini.py` — init, successful generation, error handling, empty responses
- [x] [test] Unit test `Incident` model — create, close, is_open, to/from dict
- [x] [test] Unit test `StatusTracker` — observe, episode detection, open/close incident
- [x] [test] Unit test cooldown behaviour (in `test_charm_status.py`)
- [x] [test] Unit test log line bounding (`_tail_lines`)
- [x] [test] Unit test plan-driven collection — log files, processes, systemd units, network ports, env vars
- [x] [test] Unit test plan-driven collection — empty plan, missing sections
- [x] [test] Unit test report generation — background sections
- [x] [test] Unit test report generation — plan-driven sections
- [x] [test] Unit test `write_event` audit logging
- [ ] [test] Add fake provider for AI tests

### 1.11. Optimize data gathering and LLM context

- [x] [python] Reduce charm config in report to only option name and default value
- [x] [python] Filter unit logs to error/warning lines with context window around last match
- [x] [python] Parse diagnostics.json and pass to collect_context for plan-driven collection in reports
- [x] [python] Collect and append health commands output to the incident report
- [x] [python] De-duplicate repeated log lines by normalised pattern (`deduplicate_lines`)
- [x] [python] Extract shared log helpers into `logutils.py` (`tail_lines`, `filter_error_context`, `line_pattern`, `deduplicate_lines`)
- [x] [python] Collect socket statistics (`ss -antlup`) and add report section
- [x] [python] Collect firewall rules (iptables, ufw, nftables) and add report section

## 2. Phase 2 — AI-assisted Diagnosis

Integrate AI providers to analyse persisted incident evidence, identify likely root causes, and produce advisory remediation suggestions with a complete audit trail. Nothing is executed in this phase.

### 2.1. Incident suggestion

- [x] Add provider interface
- [x] Add Gemini provider
- [x] Add OpenRouter provider
- [x] Add provider config validation
- [x] Add Juju secret/token handling
- [x] Add prompt builder
- [x] Add AI response parser
- [x] Add AI-assisted Markdown report
- [x] Ensure provider failures fall back to non-AI report

### 2.2. Suggestion engine

- [x] [python] Define `Suggestion` dataclass and attach it to the incident
- [x] [python] Add `run_suggest` — build prompt, call provider, parse commands
- [x] [python] Extract remediation commands from fenced `bash` code blocks (`parse_commands`)
- [x] [python] Add a `run_act` skeleton, unreachable until the assisted-remediation phase supplies the safety controls
- [x] [charm] Block on `mode: act` — real remediation is a later phase, not this one
- [x] [charm] Add `get-suggestion` action returning description, commands, and count
- [x] [charm] Add `additional-context` action parameter injected into the prompt as authoritative
- [x] [charm] Cache the suggestion on the incident; regenerate only when the context hash or model changes
- [x] [python] Persist the suggestion in `StatusTracker` and write a `suggestion-generated` audit event

### 2.3. Token usage and cost tracking

- [x] [python] Define `UsageMetadata` (model, prompt/completion/total tokens, cost)
- [x] [python] Parse usage metadata from Gemini and OpenRouter responses
- [x] [python] Append one `usage_log` entry per LLM call in `StatusTracker`
- [x] [python] Preserve `usage_log` across incident episodes
- [x] [python] Aggregate usage with a per-model breakdown (`summarise_usage`)
- [x] [charm] Add `show-usage` action with global summary and `incident-id` filter
- [x] [python] Include usage metadata in the `suggestion-generated` audit event

## 3. Phase 3 — Kubernetes / Multi-substrate Support

Support both a machine subordinate charm and a standalone Kubernetes charm from one codebase, with the substrate-independent behaviour factored into shared code.

### 3.1. Repository restructure and shared code

- [x] [python] Create the shared Jaime library containing substrate-independent business logic
- [x] [python] Make `jaime` a namespace package so charm-local and shared modules merge at runtime
- [x] [python] Extract `CoreMixin` (`core.py`) — mode/provider enums, provider wiring, secret resolution, suggest/act invocation, usage tracking, shared action handlers, incident lifecycle state machine
- [x] [charm] Move the machine charm to `charms/machine/` and reduce `charm.py` to substrate-specific hooks
- [x] [charm] Define the substrate hook contract (`_collect_incident_context`, `_collect_report_context`)
- [x] [python] Keep machine host logic and Kubernetes API logic out of the shared incident logic
- [x] [python] Encapsulate `StatusTracker.reset()`
- [x] [python] Stop `config-changed` from overwriting an open-incident display status
- [x] [python] Remove dead code (Ops tracing report section, unused `UsageMetadata.__add__`)

### 3.2. Kubernetes charm skeleton

- [x] [charm] Add `charms/k8s/charmcraft.yaml` as a standalone (non-subordinate) charm
- [x] [charm] Add `charms/k8s/config.yaml` with `watch-applications`, `juju-api-user`, `juju-api-password`
- [x] [charm] Add `charms/k8s/actions.yaml` (`show-status`, `show-usage`, `get-suggestion`, `generate-report`, `reset`)
- [x] [charm] Add `charms/k8s/src/charm.py` monitoring on `update-status` with no relation to watched apps
- [x] [charm] Make monitoring opt-in — empty `watch-applications` monitors nothing, never "all apps"
- [x] [charm] Add per-charm `README.md` and icon for CharmHub

### 3.3. Juju controller API access

- [x] [python] Add `controller.py` — WebSocket client for the Juju controller API (stdlib + websocket-client)
- [x] [python] Derive controller address, CA cert, and model UUID from the unit's own `agent.conf`
- [x] [python] Authenticate as a dedicated read-only Juju user (`Admin.Login`)
- [x] [python] Read workload statuses via `Client.FullStatus` and filter by `watch-applications`
- [x] [python] Read an application's live config via `Application.Get` (option names, values, operator-changed flag)
- [x] [python] Negotiate facade versions from the login response
- [x] [charm] Distinguish auth failure (`BlockedStatus`) from transient controller errors (`MaintenanceStatus`)
- [x] [charm] Resolve `juju-api-password` from a Juju secret URI or plain string

### 3.4. Kubernetes context collection

- [x] [python] Add `k8s_api.py` — read-only Kubernetes API client using the pod's in-cluster service account (no `kubectl` binary)
- [x] [python] Resolve the pod for a Juju unit via the `unit.juju.is/id` annotation, with a name-convention fallback
- [x] [python] Collect pod logs per container bounded by `sinceTime` and `tailLines`
- [x] [python] Collect Kubernetes events for the pod
- [x] [python] Collect CPU/memory usage from metrics-server when available
- [x] [python] Summarise the pod (phase, node, conditions, containers, images, restarts, probes, resources, volumes)
- [x] [python] Implement the same `collect_context()` interface as the machine collector
- [x] [python] Add k8s report sections (pod, events, resource usage) and a Juju config section
- [x] [python] Handle k8s events with missing timestamps when sorting
- [x] [charm] Ship `jaime-k8s-rbac.yaml` granting `pods`, `pods/log`, `events`, and metrics read access

### 3.5. Build and packaging

- [x] [charm] Vendor `jaime-package` into each charm at build time via `.vendored/` symlink and `override-build`
- [x] [charm] Add `make pack-machine`, `make pack-k8s`, and `make pack` targets
- [x] [docs] Document both charms in the root `README.md`

### 3.6. Tests

- [x] [test] Unit test `controller.py` — `agent.conf` parsing, login, facade negotiation, status extraction
- [x] [test] Unit test `k8s_api.py` — pod resolution, logs, events, metrics, error handling
- [x] [test] Unit test the k8s collector and k8s report sections
- [x] [test] Unit test `CoreMixin` once against a dummy charm instead of duplicating per charm
- [x] [test] Keep both suites green (machine 226 tests, k8s 55 tests)

## 3.5. Phase 3.5 — Test and tooling hygiene

Done before CI so the workflow encodes the intended layout rather than the
leftovers of the Phase 3 restructure.

### 3.5.1. Repository cleanup

- [x] [test] Delete the stale root `src/` and `tests/` directories left by the Phase 3 restructure
- [x] [test] Keep `charms/*/.vendored/` — it feeds each charm's pytest `pythonpath`, while the Makefile's `_jaime-package` copy feeds charmcraft

### 3.5.2. Test layout

- [x] [test] Adopt the `tests/unit/` and `tests/integration/` layout `AGENTS.md` documents
- [x] [test] Move shared-library tests out of `charms/machine/tests/` into `tests/unit/`
- [x] [test] Move `test_controller.py` and `test_core.py` out of `charms/k8s/tests/` into `tests/unit/`
- [x] [test] Leave only genuinely charm-specific tests under `charms/*/tests/`
- [x] [test] Make `test_core.py` declare its config inline instead of implicitly inheriting `charms/k8s/config.yaml` from the working directory

### 3.5.3. Test entrypoints

- [x] [test] Fix root `tox.ini`, which pointed at a `tests/` and `src/` that no longer existed
- [x] [test] Fix root `pyproject.toml`, whose `testpaths` silently skipped the k8s suite
- [x] [test] Add `shared`, `machine`, `k8s`, `lint` and `integration` tox environments
- [x] [test] Rewrite `scripts/test.sh` to run all three suites and report per-suite results
- [x] [test] Keep `tests/integration` out of every default test path

### 3.5.4. Lint

- [x] [project] Add `ruff` config to the root `pyproject.toml`
- [x] [project] Set `line-length = 120` to match existing style rather than force a rewrap
- [x] [project] Exempt test files from `E501`, since they embed fixture data as literals
- [x] [python] Fix the findings: unused imports, unsorted imports, ambiguous `l` identifiers, two unused locals, one misplaced import block in `charms/machine/src/jaime/collector.py`

### 3.5.5. Coverage gap

- [x] [test] Add `tests/unit/providers/test_openrouter.py`; the provider had no tests at all

## 4. Phase 4 — Improving user experience

Make both charms pleasant to build, deploy and read output from.

### 4.1. Build and packaging

- [x] [project] Make `pack-machine` and `pack-k8s` write distinct artifacts that never delete each other's
- [x] [project] Add `pack-all` producing both charms in one run
- [x] [project] Stop `clean` from wiping `.venv/` and `.tox/`; split into `clean` and `distclean`
- [x] [project] Fix the `deploy` and `remove` targets: machine-only assumptions and the `PRINCIPLE_CHARM` typo
- [x] [project] Build into `dist/` so artifacts are collectable by CI
- [x] [project] Add `deploy-k8s` and `remove-k8s`, and validate `PRINCIPAL_CHARM` before packing

### 4.2. Kubernetes deployment guidance

- [x] [charm] Add an action that emits the exact setup steps (RBAC, Juju user, secrets, config), with the observer password auto-generated via `openssl rand -hex 16`, the model name pre-filled, the RBAC applied directly from the repository URL
- [x] [charm] Block when a configured `watch-applications` name does not exist on the model.
- [x] [charm] Block until prerequisites are verified: controller authenticated and Kubernetes API readable, checked even when `watch-applications` is empty
- [x] [charm] Add an explicit RBAC preflight check instead of today's silent debug-level failure
- [x] [docs] Fix `charms/k8s/README.md` `grant-secret` target — it takes an application, not a model
- [x] [docs] Document granting the AI token secret to the application

### 4.3. Machine charm controller access

**Accepted.** The machine charm may authenticate to the Juju controller as an
external client. This lets it watch units it is not related to. The decision,
the rejected filesystem alternative and the costs are recorded in
`ARCHITECTURE.md` under Phase 4.

Scope is bounded by reach. A machine unit monitors only units on its own host,
because that is all its collectors can describe truthfully. See
[Monitoring scope](ARCHITECTURE.md#monitoring-scope).

`watch-applications` selects what to watch. Empty means the principal only, and
needs no credentials. Named applications add co-located units of those
applications. `*` adds every co-located unit. The principal is always watched.

- [x] [project] Record the decision and tradeoff in `ARCHITECTURE.md`
- [x] [docs] Update the `AGENTS.md` rule and the `ARCHITECTURE.md` statements it reverses
- [ ] [charm] Add `watch-applications` to the machine charm, with the same name and opt-in rule as the k8s charm
- [ ] [charm] Support `*` as the explicit "every co-located unit" value
- [ ] [charm] Add `juju-api-user` and `juju-api-password`, required only when `watch-applications` is non-empty
- [ ] [charm] Read unit status from the Juju controller API
- [ ] [charm] Enumerate co-located units from `/var/lib/juju/agents/unit-*`, reusing the discovery code on the `experiment-monitor-subordinates` branch
- [ ] [charm] Always watch the principal, whatever `watch-applications` is set to
- [ ] [charm] Ignore named applications that have no unit on this host, and say so in the unit status
- [ ] [charm] Block when credentials are missing or rejected, mirroring the k8s prerequisite check from 4.2
- [ ] [test] Cover the credential-free default, name filtering, `*`, and deduplication
- [ ] [docs] Document the host-only limit in `README.md`, so the boundary is not mistaken for a bug

### 4.4. Config consistency

4.3 and 4.6 each move divergent options into the shared set: `watch-applications`,
`juju-api-user` and `juju-api-password` from 4.3, `diagnostics` from 4.6.

Assert consistency over the *intersection* of the two charms' options, not a
hardcoded key list. A key moving into the shared set is then covered
automatically, and this task does not need rewriting each time.

The description alignment below touches only keys that are already shared, so it
is safe to do at any point.

- [ ] [test] Assert shared option keys, types and defaults match across both charms, computed as the intersection rather than hardcoded
- [ ] [charm] Align the drifted descriptions: `api-token`, `watch-statuses`, `log-window-minutes`, `report-dir`

### 4.5. Report content

- [ ] [python] Add snap and service detail to machine incident reports
- [ ] [python] Add further pod and container detail to k8s incident reports
- [ ] [python] Bound every addition by time, lines or bytes and keep it within `max-context-lines`

### 4.6. Kubernetes diagnostics plan parity

The machine charm generates an AI diagnostics plan on `principal-relation-joined`
and passes it to its collector. The k8s charm does neither: `collect_context`
in `charms/k8s/src/jaime/collector.py` accepts a `diagnostics_plan` argument and
ignores it, and `charms/k8s/config.yaml` has no `diagnostics` option at all. So
k8s pod collection is a fixed set with no plan-driven extensibility. Promoted
from the `ARCHITECTURE.md` ideas list because it gates the CharmHub release.

- [ ] [charm] Add a `diagnostics` config option to `charms/k8s/config.yaml`
- [ ] [charm] Generate a diagnostics plan for watched applications and persist it
- [ ] [python] Pass the plan into the k8s `collect_context` and honour it
- [ ] [test] Cover plan-driven k8s collection, matching the machine charm's tests

### 4.7. Monitoring transparency

Both charms report `Ready` without saying what they watch. An operator cannot
tell a healthy charm from a misconfigured one that is watching nothing. This
gets worse once the machine charm can watch several applications.

- [ ] [charm] Name the monitored applications in the machine charm's active status
- [ ] [charm] Name the monitored applications in the k8s charm's active status
- [ ] [charm] Report configured applications that matched no unit, rather than ignoring them silently
- [ ] [charm] Include the resolved monitored set in the `show-status` action output
- [ ] [test] Cover the status text for none, one and several applications

## 5. Phase 5 — CI/CD, integration tests and CharmHub release

### 5.1. Continuous integration

- [x] [test] Run both unit suites and lint on every pull request
- [x] [test] Run all three suites — shared, machine, k8s — as a matrix
- [x] [test] Pack both charms on CI and upload them as artifacts
- [x] [test] Gate packing behind lint and unit so slow LXD work only runs when the fast checks pass

### 5.2. Integration tests

- [x] [test] Deploy both charms, inject a fault, assert incident → report → suggestion
- [x] [test] Cover the flapping-workload case that unit tests now guard
- [x] [test] Cover missing Kubernetes RBAC and rejected controller credentials
- [x] [test] Assert the API token never reaches reports or the audit log
- [x] [test] Assert the non-AI fallback still produces a report
- [ ] [test] Run the integration suite against a real controller and fix what the first run surfaces
- [ ] [test] Add the k8s integration job to CI once a MicroK8s runner is available
- [ ] [test] Cover a realistic principal such as `postgresql 16/stable`, which is on ubuntu@24.04. The suite currently uses `any-charm` with `status-set`, which is deterministic but exercises no real workload, so Jaime's host collectors are never run against a genuine service.

### 5.3. Release

Gated on 4.2, 4.4 and 4.6.

- [ ] [project] Publish both charms to CharmHub with tracks and channels
- [ ] [docs] Per-charm CharmHub page content

## 6. Phase 6 — Clustered operation for machine charms

Today a subordinate Jaime unit runs per principal unit, so a multi-unit application produces one independent incident, one LLM call and one report per unit, with no view of the cluster.

### 6.1. Leader aggregation

- [ ] [charm] Add a peer relation to the machine subordinate
- [ ] [charm] Followers publish compacted local context; the leader aggregates it
- [ ] [charm] Make one LLM call per cluster incident, owned by the leader
- [ ] [charm] Keep incident state in the peer application databag so it survives leader change
- [ ] [python] Leader-owned usage accounting across all units
- [ ] [python] Define the aggregation window across independent `update-status` cadences
- [ ] [python] Keep unit-level detection; add cluster-level aggregation on top
