# Enable AI-assisted diagnosis

In `suggest` mode, Jaime sends the already-written incident report to an AI
provider and attaches the returned root-cause description and a single
suggested command to the incident. No command is executed.

## Store the token as a Juju secret

```bash
juju config <application> mode=suggest

SECRET_URI=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token <application>

juju config <application> provider=gemini api-token="${SECRET_URI}"
```

Grant the secret to the **application** (`jaime` or `jaime-k8s`), not the model.
The token is stored as a Juju secret and never written to plain config.

For OpenRouter, use `provider=openrouter` and optionally `model`; an empty
`model` uses the provider default.

For development only, a plain token string is accepted:

```bash
juju config <application> api-token="<your-token>"
```

## Providers

| Provider | Default model |
|---|---|
| `gemini` | `gemini-2.5-flash` |
| `openrouter` | `~deepseek/deepseek-v4-flash-latest` |

A provider is chosen by name; the token must match it. Connectivity is checked
when configuration changes, so a bad token surfaces as a blocked status rather
than a failed report.

On the machine charm, a provider is also used once to generate the diagnostics
plan when the principal relation is joined - configure it **before** relating,
or the plan is empty until you remove and re-add the relation.
