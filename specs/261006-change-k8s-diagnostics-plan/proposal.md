# Proposal: Kubernetes diagnostics plan

Type: change
Status: proposed

Tracks `TASKS.md` 4.6. Gates the 0.1.0 CharmHub release (5.3).

## Why

The k8s charm takes no diagnostics plan. `collect_context` in
`charms/k8s/src/jaime/collector.py` accepts a `diagnostics_plan` argument and
ignores it, the charm never passes one, and `charms/k8s/config.yaml` has no
`diagnostics` option. Operators cannot tell Jaime which containers matter or
what a healthy pod spec should declare, so k8s collection is a fixed set.

Full parity with the machine plan is not achievable. Jaime reads the Kubernetes
API but does not exec into workload containers, so four of the six machine plan
sections (`log_files`, `processes`, `systemd_units`, `health_commands`) cannot
be observed. This change scopes the k8s plan to what the API can show.

## What changes

- The k8s charm gains a `diagnostics` config option: a JSON object keyed by
  application name, each value a k8s-scoped plan.
- The k8s collector honours the plan: container selection, extra log patterns,
  expected env variable names and expected container ports.
- Plan check results appear in the report.
- An invalid plan blocks the unit with a clear message and a JSONL error event,
  as on the machine charm.

## Not changing

- No exec into workload containers and no health commands. Command execution is
  gated by Phase 8 (8.1).
- No AI-generated k8s plan (see `design.md`, Decision 3).
- The machine plan schema and machine behaviour.
- With no plan, k8s collection and reports are unchanged.
- No new RBAC permissions.

## Impact

- `charms/k8s/config.yaml`, `charms/k8s/src/charm.py`,
  `charms/k8s/src/jaime/collector.py`
- `jaime-package/jaime/diagnostics.py` (new k8s validator),
  `jaime-package/jaime/report.py` (declared-port status, plan-containers section)
- `tests/unit/test_config_consistency.py` (`diagnostics` becomes
  substrate-specific)
- `docs/config.md`, `ARCHITECTURE.md`, `CHANGELOG.md`
