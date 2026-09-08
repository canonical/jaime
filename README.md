# Jaime - Juju AI Medic Engine

Jaime is a Juju diagnostic and incident reporting engine, available in two variants:
- **machine subordinate** (`charms/machine/`) — co-located with a principal machine charm
- **Kubernetes standalone** (`charms/k8s/`) — runs as its own pod and monitors other applications in the same Juju model

Jaime observes and diagnoses. It does not remediate: `act` mode is blocked, and
AI output is always advisory.

- deploy Jaime alongside a principal machine charm, for example PostgreSQL or MySQL, or in a k8s model
- monitor the unit status from Juju context via `update-status`
- detect unhealthy states such as `error` or `blocked`
- wait for a configurable timeout before creating an incident report
- collect diagnostics by iterating the monitoring plan, or fall back to broad commands (`ps aux`, `ss -tlnp`, etc.)
- write structured JSONL audit logs
- optionally call an AI provider (Gemini or OpenRouter) for diagnosis suggestions

## Incident flow

```text
Jaime
→ identifies the unit(s) to monitor
→ checks unit status on every update-status
→ detects watched status: error/blocked
→ tracks how long the unit remains unhealthy
→ after failure-timeout, opens an incident
→ loads diagnostics plan (or uses broad fallback)
→ collects per-plan context (logs, processes, systemd, ports, env vars, health commands)
→ collects background context (juju logs, charm config, disk, memory)
→ writes Markdown report from the collected evidence
→ writes JSONL audit events
→ in suggest mode, sends the stored report to the AI provider and attaches the suggestion
→ respects cooldown before next report
→ closes incident on recovery
```

The unhealthy timer is anchored to when Jaime first saw the unit go unhealthy,
not to Juju's `since` timestamp. A workload retrying in a loop re-sets its status
on every hook, so relying on Juju's value would reset the timer indefinitely and
never open an incident.

## Quickstart (machine charm)

Order matters. The diagnostics plan is generated **once**, when the principal
relation is joined, and `config-changed` does not regenerate it. Configure the AI
provider *before* relating, or the plan will be empty until you remove and re-add
the relation.

```bash
# Deploy Jaime from CharmHub
juju deploy jaime

# Deploy a principal charm (e.g. postgresql)
juju deploy postgresql --channel 16/stable

# Optional but do it now, not later: enable AI-assisted diagnosis
SECRET_URI=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime
juju config jaime mode=suggest provider=gemini api-token="${SECRET_URI}"

# Relate last, so the diagnostics plan is generated with the provider available
juju relate postgresql jaime

# Monitor
juju status
juju run jaime/0 show-status
```

To update an existing deployment:

```bash
juju refresh jaime
```

The token is stored as a Juju secret and never written to plain config. For
OpenRouter, use `provider=openrouter` with the same secret URI. 

To enable AI-assisted suggestions (optional):

```bash
juju config jaime mode=suggest

# Store the token as a Juju secret (token is never stored in plain config)
SECRET_URI=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime

# Configure provider and point api-token at the secret URI
juju config jaime provider=gemini
juju config jaime api-token="${SECRET_URI}"

# Or for OpenRouter
juju config jaime provider=openrouter
juju config jaime api-token="${SECRET_URI}"
```

For development only, a plain token string is also accepted:

```bash
juju config jaime api-token="<your-token>"
```

If the plan came out empty because the provider was configured after relating:

```bash
juju remove-relation postgresql jaime && juju relate postgresql jaime
```

## Actions

```bash
juju run jaime/0 diagnose              # Basic principal info
juju run jaime/0 collect-context       # Collect and return context bundle
juju run jaime/0 generate-report       # Generate report for current open incident
juju run jaime/0 get-suggestion        # Get AI suggestion for current incident
juju run jaime/0 show-status           # Show monitoring state for all units
juju run jaime/0 reset                 # Clear all incidents and start fresh
juju run jaime/0 show-usage            # Show LLM API usage (tokens, cost...) per model
```

`get-suggestion` accepts `additional-context`, which is injected into the prompt
and treated as authoritative for the diagnosis:

```bash
juju run jaime/0 get-suggestion \
  additional-context="Disk was resized 20 min ago; pgdata is on /dev/sdb1"
```

The suggestion is cached on the incident and only regenerated when that context
or the configured model changes, so you can iterate by editing the text.

## Configuration

| Key | Default | Description |
|---|---|---|
| `mode` | `observe` | `observe`, `suggest`, or `act` |
| `provider` | `none` | AI provider (`none`, `gemini`, or `openrouter`) |
| `api-token` | `""` | Juju secret reference for the AI token |
| `watch-statuses` | `error,blocked` | Statuses that trigger an incident |
| `failure-timeout-minutes` | `5` | How long a status must persist before reporting |
| `cooldown-minutes` | `30` | Min time between reports for the same incident |
| `log-window-minutes` | `30` | How far back to collect logs |
| `max-context-lines` | `500` | Max lines per collected file/section |
| `diagnostics` | `""` | JSON monitoring plan, machine charm only (empty = AI-generated on relation) |

See `charms/machine/config.yaml` and `charms/k8s/config.yaml` for the full reference.

## Diagnostics plan

The diagnostics plan drives what gets collected. It can be:

1. **AI-generated** — on `principal-relation-joined`, Jaime calls Gemini to build a plan for the workload
2. **Manually configured** — set `diagnostics` config to a JSON monitoring plan
3. **Empty** — Jaime falls back to broad commands (`ps aux`, `ss -tlnp`, `systemctl --failed`)

Each plan section (`log_files`, `processes`, `systemd_units`, `network.ports`, `env_variables`, `health_commands`) is iterated by the collector, and results appear in the report with status icons (✓/✗).

See `examples/diagnostics.json` for a sample plan and `examples/report.md` for the generated report output.

## Modes

### observe (default)

Collect context, generate reports, write audit logs. No AI diagnosis. On the
machine charm the AI provider is still used once, to generate the diagnostics
plan when the principal relation is joined; if no provider is configured an
empty plan is written and Jaime falls back to broad commands.

### suggest

Same as observe, plus an AI diagnosis. Jaime sends the already-written report to
the provider and attaches the returned root-cause description and single
suggested command to the incident, retrievable with `get-suggestion`. The
suggestion is **not** merged into the Markdown report, and nothing is executed.

### act

**Not implemented.** Setting `mode=act` puts the charm in a blocked state and
Jaime does nothing. No command is ever executed today.

It stays blocked until command and policy allowlisting, bounded execution, a
dry-run control, a full audit trail, and rollback metadata exist. See the
assisted-remediation phase in `ARCHITECTURE.md`.

## Testing

There are three unit suites: the shared `jaime-package` library, and one per
charm. Run all of them with:

```bash
make test          # or: ./scripts/test.sh
```

Individually:

```bash
make test-shared   # jaime-package  (tests/unit/)
make test-machine  # machine charm  (charms/machine/tests/)
make test-k8s      # k8s charm      (charms/k8s/tests/)
```

Or via tox, which also provides the lint environment:

```bash
tox                # lint + all three unit suites
tox -e lint        # ruff only
```

Tests are split by what they exercise rather than by which charm hosts them.
`tests/unit/` covers the shared library and sees only `jaime-package` on its
`pythonpath`, so it cannot accidentally depend on a charm-local module.
`charms/*/tests/` holds only substrate-specific tests.

### Integration tests

`tests/integration/` deploys real charms against a real Juju controller and
drives a real fault through the incident → report → suggestion chain. These are
excluded from every default test path and never run as part of `make test`.

```bash
make integration           # both substrates; packs the charms first
make integration-machine   # needs an LXD controller
make integration-k8s       # needs a MicroK8s controller
```

Set `JAIME_TEST_API_TOKEN` (and optionally `JAIME_TEST_PROVIDER`, default
`gemini`) to exercise the AI paths. With no token the provider-dependent tests
skip and the non-AI fallback path is tested instead. Pass `--keep-models` to
leave the temporary Juju model up for post-mortem inspection.

## Building

```bash
make pack-all      # both charms into dist/
make pack-machine  # machine charm only
make pack-k8s      # k8s charm only
make clean         # remove build output
make distclean     # also remove .venv/ and .tox/
```

## Kubernetes charm (jaime-k8s)

The k8s variant runs as a standalone pod and monitors other applications in
the same Juju model. Workload statuses come from the **Juju controller API**;
pod logs/events/metrics come from the **Kubernetes API** via the pod's
in-cluster service account (no `kubectl` binary).

The machine charm discovers its principal through a relation; the k8s charm
has no relation, so it needs read access to the model's controller API.

### Deploy

The application **must** be named `jaime-k8s`: the RoleBinding in
`jaime-k8s-rbac.yaml` names that ServiceAccount.

```bash
juju deploy jaime-k8s
```

### Actions

```bash
juju run jaime-k8s/0 show-setup-steps     # print the exact setup steps for this charm
juju run jaime-k8s/0 show-status          # monitoring state
juju run jaime-k8s/0 generate-report      # report for the open incident
juju run jaime-k8s/0 get-suggestion       # AI diagnosis for the open incident
juju run jaime-k8s/0 show-usage           # Show LLM API usage (tokens, cost...) per model
juju run jaime-k8s/0 reset                # clear all incidents
```

Run `show-setup-steps` right after deploying: it prints the exact commands to
give the charm read access to both APIs, hand the observer its secrets, and
monitor your applications — with the model and application names already
filled in:

```bash
juju run jaime-k8s/0 show-setup-steps
```

The steps below explain each part in detail. Both grants are required, and
they fail differently: without the Kubernetes RBAC you get a report with empty
log and event sections; without valid Juju credentials the charm reports a
blocked status.

### Grant read access to the Kubernetes API

All applications in a Juju model share one namespace, so the charm can reach
other pods there. Its default service account can only list pods; grant pod
log/event/metrics access once per model:

```bash
kubectl apply -f https://raw.githubusercontent.com/canonical/jaime/main/charms/k8s/jaime-k8s-rbac.yaml -n <model-name>
```

### Grant read access to the Juju controller API

Create a dedicated read-only user (a unit's own agent identity does not have
the `ModelRead` permission required by `Client.FullStatus`):

```bash
MODEL_NAME=<your-model>

# Create a new juju user with read access on the model
juju add-user jaime-observer
juju grant jaime-observer read ${MODEL_NAME}

# Generate a password on the spot — or set your own here
NEW_PASS=$(openssl rand -hex 16)
echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt

# Pass the username and password (as a juju secret) to jaime-k8s
SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")
juju grant-secret jaime-juju-api jaime-k8s
juju config jaime-k8s juju-api-user=jaime-observer juju-api-password="${SECRET_URI}"
```

Note that `juju grant-secret` takes the **application** name, not the model name.

### Enable AI-assisted diagnosis (optional)

```bash
AI_SECRET=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime-k8s
juju config jaime-k8s mode=suggest provider=gemini api-token="${AI_SECRET}"
```

### Choose which applications to monitor

Monitoring is **opt-in**: an empty `watch-applications` list monitors nothing.

```bash
juju config jaime-k8s watch-applications=postgresql-k8s,mysql-k8s
```

### k8s-specific configuration

| Key | Default | Description |
|---|---|---|
| `watch-applications` | `""` | Comma-separated apps to monitor (empty = none) |
| `juju-api-user` | `""` | Juju user with read access on the model |
| `juju-api-password` | `""` | Password or Juju secret URI for `juju-api-user` |

The AI provider options (`mode`, `provider`, `model`, `api-token`) work the
same as the machine charm, as do `watch-statuses`, `failure-timeout-minutes`,
`cooldown-minutes`, `log-window-minutes`, `max-context-lines`,
`report-dir`, and `audit-log-path`.

## Design principle

Jaime should be boring, auditable, and safe.

It collects facts first, produces reports second, and only attempts changes in later phases with strict allowlists, dry-run support, and explicit operator intent.

## Roadmap and vision

The direction is to grow Jaime from a reporter into a diagnostician, and only
then into something that acts — earning each step with evidence.

Today Jaime watches one signal, Juju's workload status, one unit at a time, and
hands an operator a report plus an advisory suggestion. The next steps make that
foundation trustworthy rather than broader: a deployment story that tells you
what it needs instead of failing quietly, integration tests that prove the whole
path from fault to suggestion, and a released charm on CharmHub. From there,
Jaime learns to reason about a *cluster* rather than a host, with a leader that
gathers evidence from its peers and asks the model one well-informed question
instead of each unit asking its own poorly-informed one.

Beyond that lie the harder problems: a composite health model, because a
workload can be broken while Juju still reports it `active`, so Juju's status is
a useful trigger and not the truth; and eventually assisted remediation, which
stays blocked until allowlisting, bounded execution, dry-run, audit trail, and
rollback make it safe to let an AI suggestion become an action.

The invariant across all of it: raw evidence is collected and persisted before
any model is consulted, every AI call is auditable and costed, and Jaime never
changes a system the operator did not ask it to change. See `ARCHITECTURE.md` for
the full roadmap and `TASKS.md` for the active plan.
