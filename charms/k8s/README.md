# Jaime k8s

Jaime k8s is a Juju **Kubernetes standalone** charm that runs as its own pod
and monitors other applications in the same Juju model. It detects unhealthy
workload statuses, collects bounded diagnostic context, and writes structured
incident reports. It can optionally call an AI provider (Gemini or OpenRouter)
to suggest a diagnosis. It observes and reports only; nothing is changed
without explicit operator intent.

Workload statuses come from the **Juju controller API**; pod logs, events, and
metrics come from the **Kubernetes API** via the pod's in-cluster service
account.

> **Full documentation:** <https://canonical-jaime-charm.readthedocs-hosted.com/en/latest/>

## Quickstart

```bash
# Deploy Jaime k8s from CharmHub
juju deploy jaime-k8s
```

After deploying, Jaime k8s needs a bit of setup to read the Kubernetes API and
the Juju controller. Print the exact steps with names filled in:

```bash
juju run jaime-k8s/0 show-setup-steps
```

## Actions

```bash
juju run jaime-k8s/0 show-setup-steps     # Print the exact setup steps
juju run jaime-k8s/0 show-status          # Monitoring state
juju run jaime-k8s/0 generate-report      # Report for the open incident
juju run jaime-k8s/0 get-suggestion       # AI diagnosis for the open incident
juju run jaime-k8s/0 list-incidents       # Incident list from the audit log
juju run jaime-k8s/0 reset                # Clear all incidents
```

Monitoring is opt-in: configure which applications to watch with
`watch-applications` (see the documentation).
