Jaime is a Juju **machine subordinate** charm that observes a related
principal charm on the same host, collects bounded diagnostic context when it
becomes unhealthy, and writes structured incident reports. Optionally, it
calls an AI provider (Gemini or OpenRouter) to produce a diagnosis suggestion.

## Quickstart

```bash
# Deploy Jaime from CharmHub
juju deploy jaime

# Deploy a principal charm (e.g. postgresql)
juju deploy postgresql --channel 16/stable

juju relate postgresql jaime

# Monitor
juju status
juju run jaime/0 show-status
```

To update an existing deployment:

```bash
juju refresh jaime
```

## AI-assisted reports (optional)

```bash
juju config jaime mode=suggest

# Store the token as a Juju secret (never store the token in plain config)
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

## Actions

```bash
juju run jaime/0 diagnose              # Basic principal info
juju run jaime/0 collect-context       # Collect and return context bundle
juju run jaime/0 generate-report       # Generate report for current open incident
juju run jaime/0 get-suggestion        # Get AI suggestion for current incident
juju run jaime/0 show-status           # Show monitoring state for all units
juju run jaime/0 show-usage            # Show LLM API usage (tokens, cost) per model
juju run jaime/0 list-incidents        # List incidents from the audit log, newest first
juju run jaime/0 reset                 # Clear all incidents and start fresh
```

## Modes

- **observe** (default): collect context, generate reports, write audit logs. No AI interaction.
- **suggest**: like observe, plus calls the AI provider and appends a diagnosis. Nothing is executed.
- **act**: like suggest, but executes commands returned by the AI. Gated behind explicit opt-in; all executions are audited.

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
| `diagnostics` | `""` | JSON monitoring plan (empty = AI-generated on relation) |

## Design principle

Jaime should be boring, auditable, and safe: it collects facts first, produces
reports second, and only attempts changes with explicit operator intent.
