# Watch co-located units

The machine charm always monitors its related principal, and can additionally
monitor other units on the **same host**. This is how you monitor subordinate
charms: each is co-located with its principal and nested under it in Juju
status.

Co-located units are read through the Juju controller API, so first
[grant the charm read access](../how-to/grant_controller_access.md). Then
choose what to watch with `watch-applications`:

```bash
# Watch every unit co-located with the principal
juju config jaime watch-applications="*"

# Or name specific applications
juju config jaime watch-applications=logrotated,my-subordinate
```

| Value | Watches | Status message | Credentials |
|---|---|---|---|
| `""` (default) | the principal only | omitted | not needed |
| `""` + credentials | the principal only | **captured** | recommended |
| `app1,app2` | the principal, plus co-located units of those applications | captured | required |
| `*` | the principal, plus every co-located unit | captured | required |

The principal is always watched, whether or not it is named. A configured
application with no unit on this machine is skipped silently, so run
`show-status` to see which units are actually observed. Missing or rejected
credentials put the charm in a blocked state.

Reach is bounded to this host: the collectors read the local machine, so units
on other machines are never reported on. See
[Scope and host boundary](../explanation/scope_and_host_boundary.md).

## Kubernetes

On the Kubernetes charm, `watch-applications` selects applications across the
whole model, not just one host. Monitoring is opt-in: an empty list monitors
nothing.
