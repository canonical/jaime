# Design: hotsos integration

This spec is at `Status: in-discussion`: the decisions below are proposals
awaiting review. The hotsos facts they rest on are recorded first so the review
has a common footing.

## What hotsos is

Facts checked against the hotsos source and docs:

- Canonical Solutions Engineering toolkit; Apache-2.0; on PyPI (`hotsos`) with a
  deb build (Launchpad PPA `ubuntu-support-team/hotsos`) and a snap.
- Plugin-oriented. It analyses a **data root**: the live host (`/`) or an
  unpacked sosreport, by reading known absolute paths. It is **not** a general
  log-file analyzer, so it cannot consume the logs jaime has already collected.
- Output formats `yaml`, `json`, `markdown`, `html`. `--short` and
  `--very-short` reduce each plugin to `potential-issues`; `bugs-detected`
  carries the known-bug and CVE matches. `--all-logs` (default 7 days),
  `--max-logrotate-depth <days>`, `--command-timeout`.
- Plugin catalog: `juju`, `kernel`, `kubernetes`, `lxd`, `maas`, `microcloud`,
  `mysql`, `openstack`, `openvswitch`, `rabbitmq`, `sosreport`, `storage`,
  `system`, `vault`. There is **no `postgresql` plugin** - jaime's flagship
  example application.

## Decision 1: machine subordinate only

hotsos is a host analyzer. The machine subordinate is co-located with its
principal and reads the host. The k8s charm has only API-collected pod logs and
events, with no host data root, so hotsos has nothing to read there.

Rejected:

- **The k8s charm in the same slice.** hotsos does not query the Kubernetes API
  and its Kubernetes plugin works from node on-disk data, which `jaime-k8s` does
  not have. Deferred rather than half-built.

## Decision 2: run against the live host, not jaime's collected logs

hotsos is invoked against `/` and reads the host itself. jaime's own collection
stays as it is and is complementary, not an input.

Rejected:

- **The snap.** Run as a snap against the host, hotsos prints an error and exits
  (`is_snap() and data_root == '/'`); it only accepts an unpacked sosreport. It
  cannot do live-host analysis at all.
- **Feeding jaime's collected logs to hotsos.** hotsos plugins read known
  absolute paths under a data root; they do not take arbitrary log files. A
  directory of jaime's collected snippets is not a data root and would not be
  found.

## Decision 3: only the short digest reaches the context, report and prompt

The context, report and prompt carry `--short` output only: `potential-issues`,
`bugs-detected`, and `hotsos.version`. The full per-plugin output is an
operator-only artifact.

Rejected:

- **Rendering the full per-plugin output into the report.** The full tree
  includes config-derived values from the openstack/mysql/vault plugins, and
  `redact_text` is pattern-based, not a guarantee for arbitrary config content.

## Decision 4: an explicit plugin allowlist, always `system` and `juju`

The invocation passes plugin flags explicitly: `system` and `juju` always, plus
an application plugin only when one matches the principal. For most principals
- including postgresql - there is no matching plugin, and only the generic
coverage runs.

`hotsos-plugins` (string, default empty) overrides this: empty keeps the
auto-detected set above, and a set value is used verbatim, so an operator can
add or narrow plugins without a charm change.

Rejected:

- **Running every plugin (the hotsos default).** A default run executes roughly
  thirty plugins and is too slow for a per-hook collection.
- **Replacing jaime's engine.** hotsos is application-specific and advisory;
  jaime's engine is substrate-agnostic, bounded, and must work with hotsos
  absent.

## Decision 5: distilled through 4.9 and 6.1, in a separate report section

A dedicated `## Hotsos analysis` heading is added, not merged into existing
sections. `report.py` redacts the whole rendered report on write, so 4.9 covers
the section automatically; `projection.py` then tiers it. It is Tier 1 in the
prompt projection, alongside the executive summary.

Only the short digest enters the **context** as well as the report. The report
is redacted on write, but the `collect-context` JSON bundle is not
(`charm.py` writes the raw context with no `redact_text`), so the full
per-plugin output never enters the context at all - it stays operator-only.

Rejected:

- **Merging findings into the existing sections.** The existing sections are
  jaime's raw evidence; a hotsos finding has different provenance and trust, and
  needs to drop out cleanly when hotsos is absent or unsupported.

## Decision 6: `enable-hotsos` defaults to `false`

Consistent with jaime's observe-only-by-default posture. hotsos is new, reads
host state, and adds execution time to a hook. This one option is also the only
switch for the install: enabling it is the operator's consent to obtain hotsos
(see Decision 7), so there is no separate install flag.

Rejected:

- **Default `true`.** Enables host analysis on every deployment without an
  operator decision, before the feature is proven.
- **A separate install option.** Two switches for one intent; enabling hotsos
  already implies the operator wants it obtained if absent.

## Decision 7: install model - PATH first, else a pinned isolated venv

With `enable-hotsos` true, jaime first looks for `hotsos` on `PATH`. A found
binary is used as-is and never replaced; the version actually used is recorded.
When none is found, jaime installs a **code-pinned** `hotsos` into an isolated
venv under `/var/lib/jaime/hotsos/` (`python3 -m venv` + `pip install
hotsos==<PIN>`), invoked by absolute path. The venv keeps hotsos - and the
Sphinx/Canonical-Sphinx stack it drags in - out of the charm's own environment.

Rejected:

- **A build-time dependency.** Every deployment, enabled or not, would carry
  hotsos plus its Sphinx stack; hotsos releases almost weekly, so the charm
  would need constant rebuilds to track it; and its pins could conflict with
  jaime's own dependencies.
- **Installing into the charm's venv.** Pollutes jaime's runtime dependencies.
- **The snap.** It refuses live-host analysis (`is_snap()` and `data_root ==
  '/'`), so it cannot serve this use case.
- **pipx via apt.** `pipx install` is containable with `PIPX_HOME`/
  `PIPX_BIN_DIR`, but `apt install pipx` is a host-level change; a plain venv
  achieves the same containment without apt.
- **Operator-preinstall only.** Kept as the `PATH` path, not the only path.

## Decision 8: install once, never auto-retry

A persisted marker (ops `StoredState`) records `request` (the install-affecting
config), `result` (`installed`/`failed`/`timeout`), `version` and `at`. On
`config-changed` jaime installs only when hotsos is enabled, no usable binary is
present, and no marker exists for the current `request`; a prior `failed` or
`timeout` for the same `request` is not retried. Retry happens only when the
`request` changes - toggling `enable-hotsos` off then on, or a charm upgrade
that changes the pin. `run-hotsos` runs hotsos; it never triggers an install.

Rejected:

- **Retry on every `config-changed`.** A failed install would run again on every
  hook, repeatedly stalling the unit's serial hook queue and monitoring.
- **An `install-hotsos` action.** An extra interface for a rare case; the
  config toggle already re-arms one attempt.

## Decision 9: install state stays consistent with the config

The marker is keyed on the config it was derived from, so the installed state
always matches the config that requested it. The report and audit record the
version actually used, so a `PATH` binary that differs from the pin is visible
rather than silent. The install is atomic (built in a temporary path, renamed on
success), so a venv left half-built by a timeout is treated as `failed` and
never used. A timeout or failure is non-fatal: no section, a structured JSONL
event, and no `BlockedStatus` (optionally a `MaintenanceStatus` while it runs).
Disabling is clean: the marker is retained but inactive, so re-enabling does not
reinstall while a usable binary is still present.

## Bounds and safety

- The install runs under a wall-clock timeout; a timeout or failure is skipped
  and never auto-retried (Decision 8).
- The install runs only from `config-changed`, never from `update-status`, so it
  cannot stall the monitoring cycle repeatedly.
- A hotsos run has a wall-clock timeout plus `--command-timeout`; a byte cap on
  output; the explicit plugin allowlist from Decision 4.
- hotsos absent or failing degrades to an empty result, logs at debug, and
  writes a structured JSONL event; the rest of collection is unaffected.
- Only the short digest enters the context and the report; the raw
  (`output=full`) artifact is written to a file and returned as a path, so it
  never enters the report, the context or the prompt, and `redact_text` is never
  relied on for it.
- No incident-window anchoring: hotsos's window is a coarse 24h/day-granularity
  toggle, not a `--since`. Version and CVE findings are timeless; log-tally
  findings are not incident-scoped.

## Edge cases

| Case | Behaviour |
|---|---|
| `enable-hotsos` false (default) | hotsos is never invoked; no section in the report |
| hotsos on `PATH` | Used as-is; the actual version is recorded; nothing is installed |
| hotsos absent, enabled | Installed once, pinned, into the isolated venv; then used |
| install exceeds the timeout, or fails (PyPI unreachable, `python3-venv` missing) | Skipped; structured JSONL event; no section; **not retried** on later hooks for the same request |
| `enable-hotsos` toggled off then on, or the pin changes on upgrade | The request changes, so one fresh install attempt is allowed |
| hotsos not installed, or exits non-zero, or times out | No section; structured JSONL event; collection continues |
| Principal's application has no matching plugin | Only `system` and `juju` run (unless `hotsos-plugins` overrides); the section says so |
| hotsos reports issues/bugs | Short digest under `## Hotsos analysis`; redacted (4.9); Tier 1 in the projection (6.1), reachable by `get-suggestion`/`ask` |
| Operator runs `run-hotsos output=full` | Raw JSON written to a file; its path returned; never in the report, context or prompt |
| Principal healthy but a known bug is present | Variant: recorded as an independent signal without opening an AI incident |

## Open questions

- **Report order.** Where `## Hotsos analysis` sits relative to the executive
  summary.
- **Proactive variant.** Whether hotsos also becomes an independent, low-frequency
  signal (next section) rather than only incident enrichment.

## Non-goals

- Automatic remediation; hotsos output is advisory evidence.
- A dedicated `hotsos` subordinate charm. It is the architecturally cleaner
  answer - moving the dependency to a purpose-built charm, matching hotsos's own
  install model, reusable by any charm - but it is a separate, cross-project
  effort and no such charm exists on CharmHub today. Recorded as a longer-term
  direction.
