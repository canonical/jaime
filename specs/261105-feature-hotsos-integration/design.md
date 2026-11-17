# Design: hotsos integration

This spec is at `Status: in-discussion`: the decisions below are proposals, and
the install model is an open question rather than a settled decision. The hotsos
facts they rest on are recorded first so the review has a common footing.

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

## Decision 3: only the short digest reaches the report and prompt

The report and prompt carry `--short` output only: `potential-issues`,
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

Rejected:

- **Merging findings into the existing sections.** The existing sections are
  jaime's raw evidence; a hotsos finding has different provenance and trust, and
  needs to drop out cleanly when hotsos is absent or unsupported.

## Decision 6: `enable-hotsos` defaults to `false`

Consistent with jaime's observe-only-by-default posture. hotsos is new, reads
host state, and adds execution time to a hook.

Rejected:

- **Default `true`.** Enables host analysis on every deployment without an
  operator decision, before the feature is proven.

## Bounds and safety

- Wall-clock timeout plus `--command-timeout`; a byte cap on output; the explicit
  plugin allowlist from Decision 4.
- hotsos absent or failing degrades to an empty result, logs at debug, and
  writes a structured JSONL event; the rest of collection is unaffected.
- The raw (`output=full`) artifact is written to a file and returned as a path;
  it never enters the report or the prompt, so `redact_text` never sees it and
  it is not relied on for redaction.
- No incident-window anchoring: hotsos's window is a coarse 24h/day-granularity
  toggle, not a `--since`. Version and CVE findings are timeless; log-tally
  findings are not incident-scoped.

## Edge cases

| Case | Behaviour |
|---|---|
| `enable-hotsos` false (default) | hotsos is never invoked; no section in the report |
| hotsos not installed, or exits non-zero, or times out | No section; structured JSONL event; collection continues |
| Principal's application has no matching plugin | Only `system` and `juju` run; the section says so |
| hotsos reports issues/bugs | Short digest under `## Hotsos analysis`; redacted (4.9); Tier 1 in the projection (6.1), reachable by `get-suggestion`/`ask` |
| Operator runs `run-hotsos output=full` | Raw JSON written to a file; its path returned; never in the report or prompt |
| Principal healthy but a known bug is present | Variant: recorded as an independent signal without opening an AI incident |

## Open questions

- **Install model.** pip in the charm venv (contained, but drags the Sphinx and
  Canonical-Sphinx stack into every deployment) vs. an operator-preinstalled
  binary gated on `shutil.which("hotsos")` (pollutes only opted-in machines).
  Unresolved.
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
