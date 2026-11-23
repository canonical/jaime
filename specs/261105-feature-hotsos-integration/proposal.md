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

- `enable-hotsos` (bool, default `false`) is the single switch. When true,
  jaime runs hotsos against the live host during collection and adds a
  `## Hotsos analysis` short-digest section to the report.
- Install is PATH-first: a `hotsos` already on `PATH` is used as-is; otherwise
  jaime installs a code-pinned `hotsos` into an isolated venv under
  `/var/lib/jaime/hotsos/`, bounded by a wall-clock timeout and attempted once.
- `hotsos-plugins` (string, default empty) selects the plugins: empty means
  `system`, `juju` and a matching application plugin when one exists; a set
  value is used verbatim.
- A `run-hotsos` action runs hotsos on demand: `short` (default) returns the
  digest; `full` writes the raw JSON to a file and returns its path.

## Not changing

- The generic collector stays. hotsos is additive, not a replacement for
  jaime's engine.
- No k8s support: `jaime-k8s` has no host data root, and hotsos is not a
  cluster or Kubernetes-API analyzer.
- No automatic remediation. hotsos output is advisory evidence.
- Not a build-time dependency: hotsos (and the Sphinx stack it pulls in) is
  never in the charm's own environment.
- No sosreport/`data-root` analysis: live host only for now.
- No dedicated `hotsos` subordinate charm (longer-term direction, see
  `design.md`).

## Impact

- Machine `collector.py`: a new, opt-in collection step and the install
  reconcile on `config-changed`.
- `report.py`: a new `## Hotsos analysis` section, rendered like every other
  and therefore redacted on write (4.9).
- `config.yaml`: `enable-hotsos` and `hotsos-plugins`.
- Prompt projection (6.1): `## Hotsos analysis` is Tier 1.
- `actions.yaml` / `actions.py`: the `run-hotsos` action.
- Tests, docs and the `ARCHITECTURE.md` direction note.
