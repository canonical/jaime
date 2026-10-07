# Jaime - Juju AI Medic Engine

Jaime is a Juju diagnostic and incident reporting engine. It watches workload
status, collects bounded evidence when a unit becomes unhealthy, and writes a
structured incident report. An AI provider can optionally add an advisory
diagnosis. Jaime observes and reports; it never remediates.

It ships in two variants:

- **machine subordinate** (`charms/machine/`) - co-located with a principal
  machine charm, and optionally watching other units on the same host
- **Kubernetes standalone** (`charms/k8s/`) - runs as its own pod and monitors
  other applications in the same Juju model

## Quickstart: machine subordinate

The diagnostics plan is generated once, when the principal relation is joined.
Configure the provider before relating if you want an AI-generated plan.

```bash
juju deploy jaime
juju deploy postgresql --channel 16/stable

# Optional: enable AI-assisted diagnosis now, before relating
SECRET_URI=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime
juju config jaime mode=suggest provider=gemini api-token="${SECRET_URI}"

juju relate postgresql jaime
```

Drive the workload into a watched status (`error` or `blocked` by default), wait
for `failure-timeout-minutes`, then read the incident:

```bash
juju run jaime/0 show-status
juju run jaime/0 generate-report
juju run jaime/0 get-suggestion
```

`generate-report` and `get-suggestion` act on the current open incident, and fail
if there is none. Full guide: [Installing on a machine](docs/install-machine.md).

## Quickstart: Kubernetes

The application must be named `jaime-k8s`. The charm prints the exact setup
commands for your model:

```bash
juju deploy jaime-k8s
juju run jaime-k8s/0 show-setup-steps
```

Run the printed steps (Kubernetes RBAC, a read-only Juju user, the observer
secret), then opt in to the applications to monitor and, optionally, AI
diagnosis:

```bash
juju config jaime-k8s watch-applications=postgresql-k8s

# Optional: enable AI-assisted diagnosis
AI_SECRET=$(juju add-secret jaime-token token=<your-api-token>)
juju grant-secret jaime-token jaime-k8s
juju config jaime-k8s mode=suggest provider=gemini api-token="${AI_SECRET}"
```

Drive the application into a watched status, wait for `failure-timeout-minutes`,
then:

```bash
juju run jaime-k8s/0 show-status
juju run jaime-k8s/0 generate-report
juju run jaime-k8s/0 get-suggestion
```

Full guide: [Installing on Kubernetes](docs/install-k8s.md).

## Documentation

- [Documentation index](docs/README.md)
- [Configuration reference](docs/config.md)
- [Actions reference](docs/actions.md)
- [Operations](docs/operations.md)
- [Architecture](ARCHITECTURE.md)
- [Contributing](CONTRIBUTING.md)

## License

Apache License 2.0. See [LICENSE](LICENSE).
