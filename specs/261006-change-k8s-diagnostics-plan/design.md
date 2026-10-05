# Design: Kubernetes diagnostics plan

## Decision 1: a k8s-specific schema, not the machine schema

The machine schema is host-shaped. Of its six sections only env variables and
ports can be checked from the Kubernetes API without exec. Pebble service
output goes to container stdout, so pod logs already cover most of what
`log_files` would add.

Rejected:

- **Reuse the machine schema.** Most sections would validate and then be
  silently ignored, which misleads the operator.
- **Drop 4.6.** Leaves the `diagnostics` option divergent between charms and the
  release gate unresolved.

## Decision 2: config-only, keyed by application, no persisted file

```json
{
  "<application>": {
    "containers":    ["<name>"],
    "log_patterns":  ["<regex>"],
    "env_variables": ["<NAME>"],
    "ports":         [{"port": 5432, "protocol": "tcp"}]
  }
}
```

Every section is optional.

| Section | Effect | Without it |
|---|---|---|
| `containers` | current and previous logs only for these containers; listed names absent from the pod are reported | all containers |
| `log_patterns` | lines matching these are kept in addition to error/warning lines | error/warning filter only |
| `env_variables` | each name checked against the container spec's `env` names: set or unset, never the value | not checked |
| `ports` | each checked against declared `containerPorts` (port and protocol) | not checked |

The plan is read from config at collection time. The machine charm persists
`diagnostics.json` only because its plan is AI-generated once; a config-only
plan has nothing to persist. Applications in the plan but not monitored are
ignored and logged at debug level.

The config option keeps the shared name `diagnostics` with a different format
per substrate, so `diagnostics` joins `_SUBSTRATE_SPECIFIC` in
`tests/unit/test_config_consistency.py`.

## Decision 3: no AI plan generation in 0.1.0

The k8s charm has no per-application relation-joined hook. Generating plans on
config-changed for every watched application needs a provider-cost policy, a
persistence and regeneration policy, and its own audit trail, for little value
over an operator-written plan.

Cost: k8s operators write the plan by hand. Without one, behaviour is today's
fixed collection. AI generation can be proposed later as its own change.

## Bounds and safety

- Caps, enforced by validation (over-cap is a validation error, not truncation):
  containers ≤ 10, log_patterns ≤ 10 (each ≤ 200 characters and must compile),
  env_variables ≤ 50, ports ≤ 20, applications ≤ 50.
- Env checks report set/unset only. Values, including `valueFrom` references,
  never reach the context, report or prompt.
- `log_patterns` only select lines from logs already fetched within the existing
  window; per-item, per-line and section caps still apply.
- Regexes run over bounded input only, so a pathological pattern costs at most
  `_FETCH_LINES_CAP` lines per container.

## Edge cases

| Case | Behaviour |
|---|---|
| option empty or `{}` | unchanged collection and report |
| invalid JSON or schema | `BlockedStatus("invalid diagnostics config: <first error>")`, JSONL error event, monitoring paused until fixed |
| plan names a container the pod lacks | reported not found; collection continues |
| application has a plan but no pod | existing "no pod found" path |
| plan for an unmonitored application | ignored |

## Decision 4: report rendering

Verified against `jaime-package/jaime/report.py`:

- `_append_section_env` is reusable unchanged. It reads
  `{"type": "plan", "items": [{"name", "status"}]}` and prints set/unset only.
- `_append_section_network` needs its ✓ test to also accept a `"declared"`
  status. The pod spec shows that a port is declared, not that anything is
  listening, so the k8s collector reports `declared` / `not declared` and the
  report must not claim liveness.
- A new short report section lists plan containers with ✓ or "not found"; no
  existing section covers container selection.
- The machine collector's `_PLAN_ITEM_CAPS` dict and `_capped` helper are the
  model for the k8s section limits.
