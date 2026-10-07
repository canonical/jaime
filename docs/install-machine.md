# Installing on a machine

Jaime's machine variant is a subordinate charm: it runs alongside a principal
machine charm and reads the host directly. This page covers deployment, the
optional AI diagnosis, and monitoring other units on the same host.

For every option see the [configuration reference](config.md); for the actions
see the [actions reference](actions.md).

## Deploy

Order matters. The diagnostics plan is generated once, when the principal
relation is joined, and `config-changed` does not regenerate it. To get an
AI-generated plan, configure the provider **before** relating.

```bash
juju deploy jaime
juju deploy postgresql --channel 16/stable

# Optional: enable AI-assisted diagnosis now, before relating
SECRET_URI=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime
juju config jaime mode=suggest provider=gemini api-token="${SECRET_URI}"

juju relate postgresql jaime
```

For OpenRouter use `provider=openrouter` with the same secret URI. A plain token
string is accepted for development only; the secret form is recommended and is
never written to plain config.

If the plan came out empty because the provider was configured after relating:

```bash
juju remove-relation postgresql jaime && juju relate postgresql jaime
```

## Monitoring other units on the same host

The machine charm always monitors its related principal, and can additionally
monitor other units on the **same host**. This is how you monitor subordinate
charms: each is co-located with its principal and nested under it in Juju status.

Co-located units are read through the Juju controller API, which needs a
read-only user. A unit's own agent identity lacks the `ModelRead` permission
`Client.FullStatus` requires.

```bash
MODEL_NAME=<your-model>

juju add-user jaime-observer
juju grant jaime-observer read ${MODEL_NAME}

NEW_PASS=$(openssl rand -hex 16)
echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt

SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")
juju grant-secret jaime-juju-api jaime
juju config jaime juju-api-user=jaime-observer juju-api-password="${SECRET_URI}"

# Watch every unit co-located with the principal, or name specific applications
juju config jaime watch-applications="*"
# juju config jaime watch-applications=logrotated,my-subordinate
```

| Value | Monitors | Credentials |
|---|---|---|
| `""` (default) | the principal only | not needed |
| `app1,app2` | the principal, plus co-located units of those applications | required |
| `*` | the principal, plus every co-located unit | required |

The principal is always monitored, whether or not it is named. A configured
application with no unit on this machine is skipped silently, so run
`show-status` to see which units are actually observed. Missing or rejected
credentials put the charm in a blocked state.

## Scope

A machine subordinate monitors only units on **its own host**. Its collectors
read the local machine, so a report about a unit elsewhere would carry this
host's diagnostics. Cover more machines by relating Jaime to more principals.
See [Operations](operations.md) for the full scope and its limits.
