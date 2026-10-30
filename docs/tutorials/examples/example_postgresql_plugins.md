# PostgreSQL plugin dependency misconfiguration

This is a real incident handled end to end with the machine subordinate charm
(`jaime`) and a PostgreSQL principal (`postgresql`), running in `suggest` mode.
A single configuration change puts the workload in `blocked`; Jaime opens an
incident, and `get-suggestion` finds the root cause and the one command that
fixes it. Nothing is executed automatically - the operator applies the fix.

## Scenario

A plugin is enabled without its dependency:

```bash
juju config postgresql plugin-address-standardizer-enable=true
```

PostgreSQL's address standardizer depends on PostGIS, and the charm requires the
dependency to be enabled first. The unit goes `blocked` with a generic message:
`Unsatisfied plugin dependencies. Please check the logs`.

## The incident opens

After `failure-timeout-minutes` (default 5) with the unit still `blocked`, Jaime
opens an incident and reports it in its own status:

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
jaime  test        localhost/localhost  3.6.27   unsupported  10:43:05+01:00

App         Version  Status  Scale  Charm       Channel     Rev  Exposed  Message
jaime                active      1  jaime                  35   no       incident open: postgresql/0 blocked (3405515f)
postgresql  16.15    active      1  postgresql  16/stable  1240  no

Unit           Workload  Agent  Machine  Public address  Ports     Message
postgresql/0*  blocked   idle   0        10.82.123.139   5432/tcp  Unsatisfied plugin dependencies. Please check the logs
  jaime/0*     active    idle            10.82.123.139             incident open: postgresql/0 blocked (3405515f)

Machine  State    Address        Inst id        Base          AZ                   Message
0        started  10.82.123.139  juju-d7b135-0  ubuntu@24.04  ggouzi-ThinkPad-P16  Running
```

Jaime's status message names the incident (`3405515f`), so the two units line up
at a glance.

## Ask for a suggestion

```bash
juju run jaime/0 -- get-suggestion
```

The action returns the diagnosis grounded in the incident report, with a single
suggested command:

    Running operation 388 with 1 task
      - task 389 on unit-jaime-0

    Waiting for task 389...
    cached: "true"
    command-count: "1"
    commands: juju config postgresql plugin-postgis-enable=true
    description: |-
      The unit is blocked because `plugin-address-standardizer-enable=true` requires the PostGIS extension to be enabled first, but `plugin-postgis-enable=false`. Per the charm documentation at https://canonical-charmed-postgresql.readthedocs-hosted.com/16/, address standardizer depends on PostGIS. Enable PostGIS to satisfy the plugin dependency.

      ```bash
      juju config postgresql plugin-postgis-enable=true
      ```
    generated-at: "2026-10-26T09:42:52.852709+00:00"
    incident-id: 3405515f-5cbd-414b-8d7b-47145857e519
    turn-id: "1"

The diagnosis names the exact misconfiguration (`plugin-postgis-enable=false`),
explains why it matters, and points at the charm's own documentation. Because the
same context and model were already used, the answer is served from the
incident's cache (`cached: true`) without a fresh model call.

## Apply the fix

The suggested command is the complete fix - no further steps are required:

```bash
juju config postgresql plugin-postgis-enable=true
```

## Verify the recovery

The unit returns to `active`, and Jaime closes the incident and reports `Ready`:

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
jaime  test        localhost/localhost  3.6.27   unsupported  10:43:05+01:00

App         Version  Status  Scale  Charm       Channel     Rev  Exposed  Message
jaime                active      1  jaime                  35   no       Ready: monitoring postgresql
postgresql  16.15    active      1  postgresql  16/stable  1240  no

Unit           Workload  Agent  Machine  Public address  Ports     Message
postgresql/0*  active    idle   0        10.82.123.139   5432/tcp  Primary
  jaime/0*     active    idle            10.82.123.139             Ready: monitoring postgresql

Machine  State    Address        Inst id        Base          AZ                   Message
0        started  10.82.123.139  juju-d7b135-0  ubuntu@24.04  ggouzi-ThinkPad-P16  Running
```

## Takeaways

- The generic `blocked` message told us the unit was unhealthy, but not why;
  Jaime's grounded diagnosis pointed at the specific misconfiguration.
- The suggestion was a single, safe, documented command - nothing was executed
  automatically.
- The diagnosis was cached: asking again did not spend another model call.
- For follow-up questions on the same incident, use the `ask` action instead of
  re-running `get-suggestion`; see the
  [actions reference](../../reference/actions.md).
