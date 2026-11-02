# Traefik blocked until the cluster's load balancer has an IP pool (k8s)

This is a real incident handled end to end with the Kubernetes standalone charm
(`jaime-k8s`) and `traefik-k8s`, running in `suggest` mode. The root cause is
**environmental**: the cluster's load balancer (MetalLB, configured by the
Canonical `k8s` operator) has an *empty* IP pool, so no `LoadBalancer` service
ever receives an address. The example covers two suggestions - the first only
changes the unit's status, the second steers the operator to the cluster-level
fix - and shows how widening `watch-statuses` keeps a `waiting` unit on Jaime's
radar.

## Scenario

Traefik deploys cleanly as far as its pod goes, then reports `blocked`:

```text
traefik-k8s  2.11.57  blocked   1  traefik-k8s  latest/stable  502  10.152.183.180  no  Traefik load balancer is unable to obtain an IP or hostname from the cluster.
```

The pod is fine; the `LoadBalancer` service behind it is the problem. On the
cluster node (`k8s-dev`), the load balancer configuration shows the empty pool:

```text
$ sudo k8s get load-balancer
enabled: true
cidrs: []          # <-- no range to allocate from
l2-mode: false
l2-interfaces: []
```

## The incident opens

jaime-k8s watches `error` and `blocked` by default, so it picks up the blocked
unit, times out, collects context, and opens an incident:

```text
jaime-k8s/0*    active  idle  10.1.0.223   incident open: traefik-k8s/0 blocked (58656225)
```

## First suggestion: `external_hostname`

```bash
juju run jaime-k8s/0 -- get-suggestion
```

    Running operation * with 1 task
      - task * on unit-jaime-k8s-0

    Waiting for task ...
    cached: "true"
    command-count: "1"
    commands: juju config traefik-k8s external_hostname=traefik.example.com
    description: |-
      The unit is waiting because the Kubernetes `LoadBalancer` service backing Traefik has no external IP/hostname yet. The pod and containers are Running/Ready, so this is not a workload crash - the cluster is not providing a load balancer address. The `external_hostname` config does not bypass the charm's requirement for the LoadBalancer service to be ready.

      ```bash
      juju config traefik-k8s external_hostname=traefik.example.com
      ```
    generated-at: "2026-11-02T20:53:47.176451+00:00"
    incident-id: 58656225-8d8e-4e02-8c9d-806d841c1a7c
    turn-id: "1"

Applying it moves Traefik from `blocked` to `waiting`:

```text
traefik-k8s/0*    waiting   idle  10.1.0.185   Load balancer service has not yet obtained an external address.
```

But the unit is still broken - no IP exists. With the **default** watchers
(`error,blocked`) Jaime closes the incident, even though nothing actually
recovered:

```text
jaime-k8s/0*    active  idle  10.1.0.223   Ready: monitoring traefik-k8s
```

## Keep `waiting` on the radar - and ask again

`waiting` is often transient, which is why the default watch list excludes it.
In this case it is a *stuck* waiting state, so widen the watch list so the
incident stays open:

```bash
juju config jaime-k8s watch-statuses=error,blocked,waiting
```

Now the timeout accumulates against the `waiting` unit again (note the churn
this causes: transitions in and out of `waiting` can open and close incidents
repeatedly - a trade-off worth accepting only when `waiting` is a real signal).
With the incident open again, drill in with `ask`:

```bash
juju run jaime-k8s/0 -- ask incident-id=58656225-8d8e-4e02-8c9d-806d841c1a7c \
  question='Why is the LoadBalancer service still without an external address?'
```

    Running operation 297 with 1 task
      - task 298 on unit-jaime-k8s-0

    Waiting for task 298...
    command-count: "0"
    commands: ""
    completion-tokens: "639"
    cost-usd: "0.002822"
    description: The LoadBalancer service still has no external address because the Kubernetes
      cluster has not assigned one - no load balancer provider is supplying an external
      IP. The pod and containers are Running/Ready, so Traefik itself is fine; the missing
      piece is the cluster's load balancer implementation, typically MetalLB with an available
      IP range. Check that MetalLB is enabled and that the CIDR has addresses it can allocate
      from.
    generated-at: "2026-11-02T20:54:31.378884+00:00"
    incident-id: 58656225-8d8e-4e02-8c9d-806d841c1a7c
    model: ~deepseek/deepseek-v4-flash-latest
    prompt-tokens: "22435"
    total-tokens: "23074"
    turn-id: "2"

The second turn makes the environmental cause explicit: the workload is
healthy, the **cluster's load balancer is not allocating addresses**. That is
the operator's cue to inspect cluster infrastructure, not the application.

## Apply the real fix (cluster admin)

On this Canonical Kubernetes node the load-balancer pool is set on the `k8s`
snap. Give MetalLB a CIDR to allocate from:

```bash
# on the cluster node (LXD: lxc shell k8s-dev)
sudo k8s set load-balancer.cidrs=10.82.123.240/29
```

(`k8s enable metallb --cidr <range>` is the equivalent one-shot form described
in the charm's own docs.)

## Verify the recovery

Traefik now gets an address and serves:

```text
traefik-k8s  2.11.57  active  1  traefik-k8s  latest/stable  502  10.152.183.180  no  Serving at http://10.82.123.240
jaime-k8s            active  1  jaime-k8s                    22  10.152.183.217  no  Ready: monitoring traefik-k8s
```

The whole conversation and lifecycle are retained:

```json
[
  {
    "incident_id": "58656225-8d8e-4e02-8c9d-806d841c1a7c",
    "unit": "traefik-k8s/0",
    "workload": "waiting",
    "status_message": "Load balancer service has not yet obtained an external address.",
    "opened_at": "2026-11-02T20:51:42.826628+00:00",
    "closed_at": "2026-11-02T20:54:43.915783+00:00",
    "report_path": "/var/log/jaime/reports/58656225-8d8e-4e02-8c9d-806d841c1a7c.md",
    "status": "closed"
  }
]
```

## Takeaways

- Some incidents have an **environmental root cause**: the workload and its pod
  are healthy, but the cluster infrastructure the charm depends on is missing
  (here, a load-balancer IP pool).
- The first suggestion moved the unit out of `blocked` but not into health;
  with the default `watch-statuses` that is enough for the incident to close.
  Verify the result with `show-status` rather than assuming a state change is a
  fix.
- Adding `waiting` to `watch-statuses` keeps a stuck unit tracked so Jaime can
  answer a follow-up turn - but `waiting` flickering can reopen incidents, so
  use it deliberately.
- The two-turn conversation (`get-suggestion`, then `ask`) went from a
  workaround to the correct "check the cluster's MetalLB CIDR" guidance; both
  turns are retained per incident. See the
  [actions reference](../../reference/actions.md).