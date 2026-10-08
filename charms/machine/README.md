# Jaime

Jaime is a Juju **machine subordinate** charm that observes a related principal
charm on the same host, collects bounded diagnostic context when it becomes
unhealthy, and writes structured incident reports. It can optionally call an
AI provider (Gemini or OpenRouter) to suggest a diagnosis. It observes and
reports only; nothing is changed without explicit operator intent.

> **Full documentation:** <https://canonical-jaime-charm.readthedocs-hosted.com/en/latest/>

## Quickstart

```bash
# Deploy Jaime from CharmHub
juju deploy jaime

# Deploy a principal charm (e.g. postgresql) and relate it to Jaime
juju deploy postgresql --channel 16/stable
juju relate postgresql jaime

# Monitor
juju status
```

To update an existing deployment:

```bash
juju refresh jaime
```

## Actions

```bash
juju run jaime/0 diagnose              # Basic principal info
juju run jaime/0 generate-report       # Report for the current open incident
juju run jaime/0 get-suggestion        # AI suggestion for the current incident
juju run jaime/0 show-status           # Monitoring state for all units
juju run jaime/0 list-incidents        # Incident list from the audit log
juju run jaime/0 reset                 # Clear all incidents and start fresh
```

Jaime defaults to **observe** mode. To enable AI-assisted suggestions, see the
documentation above for configuring `mode`, `provider`, and `api-token`.
