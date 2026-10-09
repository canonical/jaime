# Changelog

## [Unreleased]

## [0.1.0] - 2026-10-08

Jaime 0.1.0 is the first release to ship both a machine subordinate charm
(`jaime`) and a Kubernetes standalone charm (`jaime-k8s`) from a single
monorepo, with monitor-by-default behaviour, bounded diagnostics, structured
incident records, and optional AI-assisted diagnosis.

### Features

- Add a Kubernetes standalone charm (`jaime-k8s`) that runs as its own pod and monitors other applications in the same Juju model. Statuses come from the Juju controller API via a dedicated read-only user; pod logs/events/metrics come from the Kubernetes API via the pod's in-cluster service account. Monitoring is opt-in via `watch-applications`
- Add a `show-setup-steps` action to the k8s charm that prints the exact setup steps (RBAC, observer user, secrets, `watch-applications`) with model and application names pre-filled
- Block the k8s charm until prerequisites are verified: observer credentials authenticate, every configured application exists, and the service account can read pod logs via the shipped RoleBinding
- Add opt-in controller access to the machine charm so it can watch co-located units it is not related to (`watch-applications` or `*` for all)
- Add a `list-incidents` action to both charms, correlating audit events by incident id and reporting each incident's start, report and close timestamps
- Persist the workload status message ("reason") on the incident and at the top of the report, so operators and the LLM see Juju's own statement of what is wrong
- Add snap diagnostics (failed services only) and systemd service detail to machine reports, and container state (including `CrashLoopBackOff`/`OOMKilled`) to k8s reports

### Changes

- Restructure into a monorepo: a shared `jaime-package/` library, `charms/machine/`, and `charms/k8s/`, each vendoring the library at pack time. `jaime` is a namespace package
- Extract `CoreMixin` with the substrate-independent behaviour (provider wiring, secret resolution, suggest/act invocation, incident lifecycle). Machine charm 835 -> 262 lines, k8s charm 684 -> 188
- Build both charms for Ubuntu 22.04, 24.04 and 26.04 on amd64 and arm64, packed with the `uv` plugin
- List the principal charm's docs/source/issues links and declared actions in the report, and instruct the suggest prompt to base remediation on those official sources only
- Trim repeat noise from incident reports: collapse recurring log blocks to first occurrence plus an omission count, and omit config options whose default is empty
- Bound every collected item in time, lines or bytes, with a per-line byte cap; `max-context-lines` is now a per-item cap
- Report environment variables as set/unset only, never by value
- Change the default OpenRouter model from `deepseek/deepseek-chat` to `~deepseek/deepseek-v4-flash-latest`
- Align the shared config descriptions and defaults between the two charms, with a test covering the shared option set
- Add a GitHub Actions CI workflow (lint + unit matrix + packing), ruff config, and an integration test suite built on `jubilant`
- Adopt the `tests/unit` and `tests/integration` layout; repair the root test entrypoints and `testpaths`

### Bug fixes

- Anchor the incident timer to a Jaime-owned `unhealthy_since` instead of Juju's `since`, which was reset on every hook so the timeout was never reached
- Treat a continuous run of watched statuses as one episode, so flapping statuses cannot produce repeat reports for a single failure
- Fix `show-status` to report every monitored unit instead of overwriting with only one
- Make `collect-context` collect a real bounded context bundle instead of returning a placeholder path

## [0.0.7] - 2026-08-29

`version: "0.0.7"` was introduced alongside the Kubernetes charm, so this entry
covers everything released since 0.0.6.

### Features

- Add a Kubernetes standalone charm (`jaime-k8s`) that runs as its own pod and monitors other applications in the same Juju model. Monitoring is opt-in via `watch-applications` — empty means nothing is monitored, never "all applications"
- Read k8s workload statuses from the Juju controller API (`Client.FullStatus`) using a dedicated read-only Juju user, since a unit's own agent identity lacks `ModelRead`
- Fetch a watched application's live Juju config via `Application.Get`, including which options the operator changed
- Collect pod logs, events, and metrics through the Kubernetes API using the pod's in-cluster service account — no `kubectl` binary
- Ship `jaime-k8s-rbac.yaml` granting read access to `pods`, `pods/log`, `events`, and metrics
- Add AI token usage and cost tracking: one `usage_log` entry per LLM call, preserved across incident episodes, aggregated with a per-model breakdown by the new `show-usage` action
- Add a `get-suggestion` action with an `additional-context` parameter injected into the prompt as authoritative. Suggestions are cached on the incident and regenerated only when that context or the model changes
- De-duplicate repeated log lines by normalised pattern, so a health check failing every few seconds collapses to one entry before reaching the AI provider
- Add socket statistics (`ss -antlup`) and firewall rules (iptables, ufw, nftables) report sections

### Changes

- Restructure into a monorepo: a shared `jaime-package/` library, `charms/machine/`, and `charms/k8s/`, each vendoring the library at pack time. `jaime` is a namespace package, so shared and charm-local modules merge at runtime
- Extract `CoreMixin` (`core.py`) with the substrate-independent behaviour — provider wiring, secret resolution, suggest/act invocation, usage tracking, shared action handlers, and the incident lifecycle state machine. Machine charm 835 -> 262 lines, k8s charm 684 -> 188
- Extract shared log helpers into `logutils.py`
- Pass the Gemini API token as an `x-goog-api-key` header instead of a URL query parameter, to keep it out of proxy logs
- Accept either a Juju secret URI or a plain string for `api-token` and `juju-api-password`
- Write the status state file atomically via a temp file and rename
- Reduce the default `log-window-minutes` from 120 to 30
- Report Juju's raw status timestamp as `status_since` in the incident events, alongside the Jaime-anchored `first_seen`
- Add `status-since` to the `show-status` action output
- Remove dead code: the Ops tracing report section and the unused `UsageMetadata.__add__`
- Sync `ARCHITECTURE.md` and `TASKS.md` with the implemented state. Restructure the roadmap into seven phases, splitting AI-assisted diagnosis (Phase 2, implemented) from assisted remediation (Phase 7, future) and inserting Kubernetes/multi-substrate support as Phase 3
- Document a two-signal health model in `ARCHITECTURE.md`: Juju workload status is the current incident trigger, but a workload can be broken while Juju still reports `active`, so workload-health checks may trigger incidents in future

### Bug fixes

- Anchor the incident timer to a Jaime-owned `unhealthy_since` instead of Juju's `since`. A charm retrying in a loop re-sets its workload status on every hook, which bumps `since` and previously reset the timer, so `failure-timeout-minutes` was never reached and no incident was ever opened
- Treat a continuous run of watched statuses as one episode. Flapping between two watched statuses (e.g. `maintenance` <-> `blocked`) no longer clears the open incident and cooldown, so it cannot produce repeat reports for a single failure
- Start the log-collection window at `unhealthy_since`, so the causal error at the beginning of a failure is inside the collected window
- Persist the observation increment on every observe. Each hook is a fresh process, so writing only on episode change meant the increment was reloaded from disk and never advanced past 2
- Add the missing `Incident` import to the machine charm, which raised `NameError` on every hook
- Stop `config-changed` from overwriting an open-incident display status with a plain "Ready"
- Encapsulate `StatusTracker.reset()` instead of mutating internal state from the charm
- Sort Kubernetes events safely when `lastTimestamp` and `firstTimestamp` are both absent
- Remove the `subordinate` flag from the k8s charm, which is standalone

## [0.0.6] - 2026-07-15

### Changes

- Reduce charm config in report to only option name and default value (YAML parse + extract, drop full raw dump)
- Filter unit logs to error/warning lines with context window (±10 rows) around the last chronological match
- Remove crude `" INFO "/" DEBUG "` substring filter in favour of context-window approach

## [0.0.5] - 2026-07-15

### Features

- Add OpenRouter as a second AI provider with configurable model selection
- Add provider connectivity check (`check()`) for both Gemini and OpenRouter

### Changes

- Log AI provider call results: INFO-level success/failure per provider, DEBUG-level full response
- Improve error messages in diagnostics generation to include the provider name

## [0.0.4] - 2026-07-14

### Features

- Wire diagnostics plan into collector: iterate plan sections and collect per-item data
- Add broad fallback collection when plan is empty or missing (ps aux, ss -tlnp, etc.)
- Add plan-driven Markdown report sections (log files, processes, systemd units, network ports, env vars)
- Add unit tests for plan-driven collection and report sections (23 new tests, 171 total)

## [0.0.3] - 2026-07-14

### Features

- Implement principal status monitoring with goal-state via `StatusTracker`
- Add incident lifecycle model (`Incident` dataclass) with open/close/recovery
- Add bounded context collector (Juju logs, systemd, disk, memory)
- Add plan-driven context collection: iterate diagnostics plan and collect per-section data
- Add Markdown report generation from collected context
- Add structured JSONL audit logging
- Add `show-status` and `reset` actions
- Add suggest/act mode support (gated behind mode config)
- Add test infrastructure (pytest, pyproject.toml, tox.ini, test script)
- Add unit tests for `diagnostics.py`, `providers/`

## [0.0.2] - 2026-07-05

### Features

- Add `diagnostics` config variable for monitoring plan
- Add diagnostics JSON schema and validation
- Add AI-powered diagnostics plan generation on `principal-relation-joined`
- Add Gemini provider using REST API (stdlib only)
- Create `src/jaime/` and `src/jaime/providers/` package structure
- Fall back to empty diagnostics plan when no AI provider is configured

## [0.0.1] - 2026-06-21

### Features

- Initial machine subordinate charm skeleton
- Add `metadata.yaml` for machine subordinate charm
