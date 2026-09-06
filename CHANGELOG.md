# Changelog

## [Unreleased]

### Changes

- Change the default OpenRouter model from `deepseek/deepseek-chat` to `~deepseek/deepseek-v4-flash-latest`. The legacy model's shared free pool is routinely rate-limited upstream (HTTP 429), and v4-flash is effectively free with substantially more capacity; the `~` prefix tracks OpenRouter's latest v4-flash release
- Replace em-dashes with colons in user-facing output: unit status messages (e.g. `Ready: no apps in watch-applications`), action results, and action failure messages (`no suggestion available: mode is 'observe'`). Em-dashes remain only in docstrings, comments, debug-log lines, and report prose
- Adopt the `tests/unit` and `tests/integration` layout documented in `AGENTS.md`. Shared-library tests move out of `charms/machine/tests/` and `charms/k8s/tests/` into `tests/unit/`, leaving only genuinely charm-specific tests in the charm suites. `tests/unit` sees only `jaime-package` on its `pythonpath`, which proves the shared library does not depend on any charm-local module
- Make the `CoreMixin` test declare its config inline instead of implicitly inheriting `charms/k8s/config.yaml` from the working directory
- Repair the root test entrypoints. `tox.ini` pointed at a `tests/` and `src/` that no longer existed after the Phase 3 restructure, and the root `pyproject.toml` `testpaths` silently skipped the entire k8s suite. `tox` and `scripts/test.sh` now run all three suites
- Add `ruff` lint configuration with `line-length = 120`, chosen to match existing style rather than force a rewrap. Test files are exempt from `E501` because they embed fixture data as literals
- Add a GitHub Actions CI workflow: lint plus a three-suite unit matrix on every pull request, then charm packing gated behind them, with artifacts uploaded
- Build both charms into `dist/`. `pack-machine` and `pack-k8s` no longer delete each other's artifacts, and `pack-all` produces both in one run
- Split `make clean` from `make distclean` so cleaning no longer destroys `.venv/` and `.tox/`
- Fix the `deploy` and `remove` Makefile targets: correct the `PRINCIPLE_CHARM` typo to `PRINCIPAL_CHARM`, validate it before doing any packing work, and add `deploy-k8s` and `remove-k8s`
- Remove the stale root `src/` and `tests/` directories left behind by the Phase 3 restructure

### Features

- Change the default OpenRouter model from `deepseek/deepseek-chat` to `~deepseek/deepseek-v4-flash-latest`. The legacy model's shared free pool is routinely rate-limited upstream (HTTP 429), and v4-flash is effectively free with substantially more capacity; the `~` prefix tracks OpenRouter's latest v4-flash release
- Block the k8s charm until its prerequisites are verified: the Juju observer credentials authenticate against the controller, every configured `watch-applications` name exists on the model, and the in-cluster service account can read pod logs via the shipped RoleBinding. A missing RoleBinding becomes a `BlockedStatus` naming the RBAC (`Kubernetes RBAC missing: ...`) instead of today's silent empty report sections, and resolving it releases the unit back to active. Transient controller/API hiccups still degrade to maintenance rather than blocking bootstrap. The connectivity checks run even when `watch-applications` is empty, so an unconfigured charm is never reported ready just because nothing is monitored
- Add a `show-setup-steps` action to the k8s charm that prints the exact setup steps: Kubernetes RBAC, the read-only Juju observer user, granting the observer-password and AI-token secrets to the application, and configuring `watch-applications` and the AI provider. Read-only, pre-filled with the charm's own application and model names, link to RBAC Role and RoleBinding yaml definitions directly from the repository, and with `NEW_PASS` generated on the spot via `openssl rand -hex 16`
- Block the k8s charm when a configured `watch-applications` name is absent from the model. The check runs on `config-changed` and every `update-status`, and lists the missing names; it stays silent while the controller is not yet reachable (missing observer credentials or a transient fetch failure), so bootstrap is not blocked
- Add an integration test suite (`tests/integration/`) built on `jubilant`, covering the machine incident chain (fault → incident → report → suggestion), the flapping-workload case, recovery, the non-AI fallback path, and for Kubernetes both documented failure modes: rejected controller credentials and missing RBAC. Includes assertions that the API token never reaches a report or the audit log. Excluded from every default test path; run with `make integration`
- Add `tests/unit/providers/test_openrouter.py`. The OpenRouter provider previously had no test coverage at all

### Bug fixes

- Fix the `charms/k8s/README.md` observer block: `juju grant-secret` grants to an application, not a model, so target `jaime-k8s` instead of `${MODEL_NAME}`, and document granting the AI token secret the same way
- Fix a misplaced import block in `charms/machine/src/jaime/collector.py`, where `jaime.logutils` was imported halfway down the file
- Rename ambiguous `l` loop identifiers to `line` across the collectors and report generator

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
