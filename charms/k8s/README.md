Jaime k8s is a Juju **Kubernetes standalone** charm that runs as its own pod
and monitors other applications in the same Juju model. It detects unhealthy
workload statuses, collects bounded diagnostic context from the pod, and
writes structured incident reports. Optionally, it calls an AI provider
(Gemini or OpenRouter) to produce a diagnosis suggestion.

Workload statuses come from the **Juju controller API**; pod logs/events/metrics come from the **Kubernetes API** via the pod's in-cluster service account

## Quickstart

```bash
juju deploy jaime-k8s
```

## Actions

```bash
juju run jaime-k8s/0 show-setup-steps     # print the exact setup steps
juju run jaime-k8s/0 show-status          # monitoring state
juju run jaime-k8s/0 generate-report      # report for the open incident
juju run jaime-k8s/0 get-suggestion       # AI diagnosis for the open incident
juju run jaime-k8s/0 show-usage           # LLM API usage (tokens, cost) per model
juju run jaime-k8s/0 reset                # clear all incidents
```

Run `show-setup-steps` right after deploying: it prints the exact commands to
give the charm read access to the Kubernetes API and the Juju controller, hand
the observers their secrets, and monitor your applications — with the model
and application names already filled in:

```bash
juju run jaime-k8s/0 show-setup-steps
```

The sections below explain each step in detail.

### Grant read access to the Kubernetes API

All applications in a Juju model share one namespace, so the charm can reach
other pods there. Its default service account can only list pods; grant pod
log/event/metrics access once per model:

```bash
kubectl apply -f https://raw.githubusercontent.com/canonical/jaime/main/charms/k8s/jaime-k8s-rbac.yaml -n <model-name>
```

### Grant read access to the Juju controller API

A unit's own agent identity does not have the `ModelRead` permission required
by `Client.FullStatus`, so create a dedicated read-only user:

```bash
MODEL_NAME=<your-model>

juju add-user jaime-observer
juju grant jaime-observer read ${MODEL_NAME}

# Generate a password on the spot — or set your own here
NEW_PASS=$(openssl rand -hex 16)
echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt

# Pass the username and password (as a Juju secret) to jaime-k8s.
# grant-secret takes the application, not the model.
SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")
juju grant-secret jaime-juju-api jaime-k8s
juju config jaime-k8s juju-api-user=jaime-observer juju-api-password="${SECRET_URI}"
```

### Grant the AI token (optional)

Suggest mode needs an AI provider token. Store it as a Juju secret and grant
it to the application (`jaime-k8s`, not the model), so the plain token never
appears in the charm config:

```bash
AI_SECRET=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime-k8s
juju config jaime-k8s mode=suggest provider=gemini api-token="${AI_SECRET}"
```

### Choose which applications to monitor

Monitoring is opt-in: an empty `watch-applications` list monitors nothing.

```bash
juju config jaime-k8s watch-applications=postgresql-k8s,mysql-k8s
```

## Configuration

| Key | Default | Description |
|---|---|---|
| `watch-applications` | `""` | Comma-separated apps to monitor (empty = none) |
| `juju-api-user` | `""` | Juju user with read access on the model |
| `juju-api-password` | `""` | Password or Juju secret URI for `juju-api-user` |
| `mode` | `observe` | `observe`, `suggest`, or `act` |
| `provider` | `none` | AI provider (`none`, `gemini`, or `openrouter`) |
| `api-token` | `""` | Juju secret reference for the AI token |

The usual options also apply (`watch-statuses`, `failure-timeout-minutes`,
`cooldown-minutes`, `log-window-minutes`, `max-context-lines`,
`report-dir`, `audit-log-path`).

## Design principle

Jaime should be boring, auditable, and safe: it collects facts first, produces
reports second, and only attempts changes with explicit operator intent.
