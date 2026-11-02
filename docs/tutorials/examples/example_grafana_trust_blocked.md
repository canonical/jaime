# Grafana blocked until the charm is trusted (k8s)

This is a real incident handled end to end with the Kubernetes standalone charm
(`jaime-k8s`) and a freshly deployed `grafana-k8s` workload, running in
`suggest` mode. The Grafana charm comes up `blocked` because its Kubernetes
service account cannot patch the StatefulSet it manages; Jaime opens an
incident, and `get-suggestion` explains the RBAC limitation and the one command
that resolves it. Nothing is executed automatically - the operator applies the
fix.

## Scenario

Deploy `grafana-k8s` in the same model as jaime-k8s, and set
`watch-applications` to cover it. The workload immediately reports `blocked` -
the charm's service account is not trusted to manage its own StatefulSet:

```text
App             Version  Status   Scale  Charm           Channel  Rev  Address         Exposed  Message
grafana-k8s     12.0.2   blocked      1  grafana-k8s     2/stable  180  10.152.183.207  no       Kubernetes resources patch failed: `juju trust` this application. statefulsets.apps "grafana-k8s" is forbidden: User ...
jaime-k8s                active       1  jaime-k8s                    22  10.152.183.217  no       incident open: grafana-k8s/0 blocked (87bd4e07)

Unit                 Workload  Agent  Address    Ports  Message
grafana-k8s/0*       blocked   idle   10.1.0.59         Kubernetes resources patch failed: `juju trust` this application. statefulsets.apps "grafana-k8s" is forbidden: User ...
jaime-k8s/0*         active    idle   10.1.0.109        incident open: grafana-k8s/0 blocked (87bd4e07)
```

## The incident opens

There is no loudness here - the workload containers are actually healthy; the
`blocked` status comes from the charm's own Kubernetes resource operations
failing. The full status message pins down the exact gap:

```text
Kubernetes resources patch failed: `juju trust` this application.
statefulsets.apps "grafana-k8s" is forbidden: User
"system:serviceaccount:jaime-k8s:grafana-k8s" cannot get resource
"statefulsets" in API group "apps" in the namespace "jaime-k8s"
```

## Ask for a suggestion

```bash
juju run jaime-k8s/0 -- get-suggestion
```

    Running operation 265 with 1 task
      - task 266 on unit-jaime-k8s-0

    Waiting for task 266...
    cached: "true"
    command-count: "1"
    commands: juju trust grafana-k8s
    description: |-
      The unit is blocked because the Grafana charm's Kubernetes service account lacks RBAC permission to get/patch the `grafana-k8s` StatefulSet. The workload containers are healthy; the failure is in the charm's Kubernetes resource patch operation. The status message explicitly instructs to grant trust to the application, which gives the charm's service account the required permissions in the model namespace.

      ```bash
      juju trust grafana-k8s
      ```
    generated-at: "2026-11-02T20:08:00.635123+00:00"
    incident-id: 87bd4e07-5071-40fa-80f2-9286573be8a0
    turn-id: "1"

The suggestion separates the healthy workload from the RBAC failure and points
at `juju trust` - the documented remedy named by the charm's own status message.

## Apply the fix

Grafana's StatefulSet patch needs cluster scope on this cloud:

```bash
juju trust grafana-k8s --scope cluster
```

## Verify the recovery

The workload becomes `active` and Jaime closes the incident automatically:

```text
App             Version  Status  Scale  Charm           Channel  Rev  Address         Exposed  Message
grafana-k8s     12.0.2   active      1  grafana-k8s     2/stable  180  10.152.183.207  no
jaime-k8s                active      1  jaime-k8s                    22  10.152.183.217  no       Ready: monitoring grafana-k8s
```

`list-incidents` records the whole lifecycle:

```json
[
  {
    "incident_id": "87bd4e07-5071-40fa-80f2-9286573be8a0",
    "unit": "grafana-k8s/0",
    "workload": "blocked",
    "status_message": "Kubernetes resources patch failed: `juju trust` this application. statefulsets.apps \"grafana-k8s\" is forbidden: ...",
    "opened_at": "2026-11-02T20:07:45.133514+00:00",
    "closed_at": "2026-11-02T20:08:24.510150+00:00",
    "report_path": "/var/log/jaime/reports/87bd4e07-5071-40fa-80f2-9286573be8a0.md",
    "status": "closed"
  }
]
```

## Takeaways

- An untrusted K8s charm can sit `blocked` while its containers are perfectly
  healthy - the failure is RBAC, not the workload.
- jaime-k8s detected the blocked status through the Juju controller API, with
  no relation, and its report kept the full status message that names the fix.
- The suggestion distinguished the healthy workload from the resource-patch
  failure instead of jumping to "restart the pod".
- `juju trust` (with `--scope cluster` where needed) is the complete remedy.
  For follow-up questions, use the `ask` action; see the
  [actions reference](../../reference/actions.md).