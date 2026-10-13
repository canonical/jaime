# Jaime

Jaime is a Juju **machine subordinate** charm that observes a related principal
charm on the same host, collects bounded diagnostic context when it becomes
unhealthy, and writes structured incident reports. It can optionally call an
AI provider (Gemini or OpenRouter) to suggest a diagnosis. It observes and
reports only; nothing is changed without explicit operator intent.

> **Full documentation:** <https://canonical-jaime-charm.readthedocs-hosted.com/en/latest/>

## Quickstart

```bash
# Deploy Jaime from CharmHub (0.1/stable channel)
juju deploy jaime --channel=0.1/stable

# Deploy a principal charm (e.g. postgresql) and relate it to Jaime
juju deploy postgresql --channel 16/stable
juju relate postgresql jaime

# Monitor
juju status
```

To update an existing deployment:

```bash
juju refresh jaime --channel=0.1/stable
```

## Actions

```bash
juju run jaime/0 diagnose              # Basic principal info
juju run jaime/0 collect-context       # Write a context bundle and return its path
juju run jaime/0 generate-report       # Report for the current open incident
juju run jaime/0 get-suggestion        # AI suggestion for the current incident
juju run jaime/0 show-status           # Monitoring state for all units
juju run jaime/0 show-usage            # LLM token usage and cost per model
juju run jaime/0 list-incidents        # Incident list from the audit log
juju run jaime/0 reset                 # Clear all incidents and start fresh
```

Jaime defaults to **observe** mode. To enable AI-assisted suggestions, see the
documentation above for configuring `mode`, `provider`, and `api-token`.

### Local OpenAI-compatible providers

To use a local server that speaks the OpenAI Chat Completions protocol (e.g.
Ollama) instead of a hosted provider, configure `provider=remote` with
`provider-remote-url` and an exact model tag. The charm runs inside the LXD
container, so `provider-remote-url` must name the host address as seen from
the container, and Ollama must listen beyond localhost (`OLLAMA_HOST=0.0.0.0`):

```bash
juju config jaime mode=suggest provider=remote model=llama3.2 \
  provider-remote-url=http://<host-ip>:11434/v1
```

No `api-token` is needed for a token-less local server.
