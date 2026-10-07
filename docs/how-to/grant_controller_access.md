# Grant the charm read access to the controller API

Both charms read workload status through the Juju controller API with a
**read-only observer user**. A unit's own agent identity does not have the
`ModelRead` permission that `Client.FullStatus` requires, so the charm needs a
dedicated user created for it.

Once granted, the observer also lets Jaime capture a workload's **status
message** — the "reason" Juju reports for an unhealthy unit, for example
`Please initialize OpenBao or integrate with an auto-unseal provider` — and
include it at the top of every incident report.

## Create the observer user

```bash
MODEL_NAME=<your-model>

juju add-user jaime-observer
juju grant jaime-observer read ${MODEL_NAME}

NEW_PASS=$(openssl rand -hex 16)
echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt

SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")
juju grant-secret jaime-juju-api <application>
juju config <application> juju-api-user=jaime-observer juju-api-password="${SECRET_URI}"
```

`juju grant-secret` takes the **application** name, not the model name, so:

- on the machine charm, grant to `jaime`
- on the Kubernetes charm, grant to `jaime-k8s`

## Why it is recommended on the machine charm

On Kubernetes the status always comes from the controller API, so the message
is captured automatically. On the machine charm the principal's status is
normally read from the local `goal-state` hook tool, which carries the status
name and timestamp but **not the message**. Granting the observer is optional
but recommended: without it, reports about the principal simply omit the
message. It is also what makes watching co-located units possible — see
[Watch co-located units](../how-to/watch_co_located_units.md).

On the Kubernetes charm, this grant is **required**, not optional: statuses
come from the controller API, so the charm cannot monitor at all without it.
