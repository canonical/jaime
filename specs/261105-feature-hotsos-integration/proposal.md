# Proposal: Integrate hotsos as a diagnostic provider

Type: feature
Status: in-discussion

Tracked as an idea in `TASKS.md`; no release assigned. This spec is under
discussion: the design is not agreed and there is deliberately no `tasks.md`
yet. Nothing here is committed to.

## Why

jaime's collector is generic (`ps`, `ss`, `df`, `free`, `systemctl`, unit logs,
journal). That describes a fault, but leaves the diagnosis - known bug,
misconfiguration, resource limit - to a human or the LLM. hotsos performs
deterministic, expert analysis of a host or sosreport: per-plugin summaries
plus `potential-issues` and `bugs-detected` (for example, CVE-matched Juju
versions). Feeding that into the report and prompt would:

- enrich the report with deterministic, non-hallucinated findings;
- validate whether a known bug or CVE is present on the machine;
- ground `get-suggestion` in verified facts instead of raw-log inference;
- and, in a second variant, surface host-hygiene concerns while the workload is
  still healthy - proactively and with no AI provider configured.

## What changes

Candidates, pending the design:

- a hotsos collection path and/or a `run-hotsos` action;
- an opt-in `## Hotsos analysis` report section;
- a config option to enable hotsos.

## Not changing

- The generic collector stays. hotsos is additive, not a replacement for
  jaime's engine.
- No k8s support: `jaime-k8s` has no host data root, and hotsos is not a
  cluster or Kubernetes-API analyzer.
- No automatic remediation. hotsos output is advisory evidence.

## Impact

To be determined once the design converges: collector, config, report and
projection, tests and docs.
