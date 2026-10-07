# Configure a diagnostics plan

The diagnostics plan drives what gets collected when an incident opens.

## Machine charm

The plan describes the principal's log files, processes, systemd units,
network ports, environment variables, and optional health commands. It can be:

1. **AI-generated** — on `principal-relation-joined`, Jaime calls the configured
   provider to build a plan for the workload. Configure the provider before
   relating, or the plan is empty.
2. **Manually configured** — set the `diagnostics` option to a JSON monitoring
   plan.
3. **Empty** — Jaime falls back to broad commands.

The plan is generated once and stored at `/var/lib/jaime/diagnostics.json`;
`config-changed` does not regenerate it.

## Kubernetes charm

On Kubernetes, `diagnostics` is a JSON object keyed by application name. For
each application you can set `containers`, `log_patterns`, `env_variables`,
and `ports` to check the pod against what the Kubernetes API can observe
without `exec`. Empty means the fixed pod collection is used; an invalid plan
blocks the unit.

The plan is read from the `diagnostics` option at collection time.

## Format

See the `diagnostics` option in the [configuration
reference](../reference/configuration.md) for both plan formats, and
`examples/diagnostics.json` in the repository for a sample machine plan.
