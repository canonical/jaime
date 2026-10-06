# Installing on Kubernetes

Jaime's Kubernetes variant is a standalone charm: it runs as its own pod and
monitors other applications in the same Juju model. Workload statuses come from
the **Juju controller API**; pod logs, events and metrics come from the
**Kubernetes API** through the pod's in-cluster service account (no `kubectl`
binary).

For every option see the [configuration reference](config.md); for the actions
see the [actions reference](actions.md).

The application **must** be named `jaime-k8s`: the shipped RoleBinding names that
ServiceAccount.

```bash
juju deploy jaime-k8s
```

## Run the setup steps

The charm prints the exact commands for your model and application names:

```bash
juju run jaime-k8s/0 show-setup-steps
```

The steps, explained below.

### Grant read access to the Kubernetes API

All applications in a Juju model share one namespace, so the charm can reach
other pods there. Its default service account can only list pods; grant pod
log/event/metrics access once per model:

```bash
kubectl apply -f https://raw.githubusercontent.com/canonical/jaime/main/charms/k8s/jaime-k8s-rbac.yaml -n <model-name>
```

### Grant read access to the Juju controller API

Create a dedicated read-only user. A unit's own agent identity does not have the
`ModelRead` permission required by `Client.FullStatus`.

```bash
MODEL_NAME=<your-model>

juju add-user jaime-observer
juju grant jaime-observer read ${MODEL_NAME}

NEW_PASS=$(openssl rand -hex 16)
echo "$NEW_PASS" | juju change-user-password jaime-observer --no-prompt

SECRET_URI=$(juju add-secret jaime-juju-api password="$NEW_PASS")
juju grant-secret jaime-juju-api jaime-k8s
juju config jaime-k8s juju-api-user=jaime-observer juju-api-password="${SECRET_URI}"
```

`juju grant-secret` takes the **application** name, not the model name.

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