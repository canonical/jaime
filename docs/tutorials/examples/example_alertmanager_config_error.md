# Malformed Alertmanager configuration file (k8s)

This is a real incident handled end to end with the Kubernetes standalone charm
(`jaime-k8s`) and a single-unit `alertmanager-k8s` workload, running in
`suggest` mode. A mistyped `config_file` makes the charm's `update-status` hook
fail; the unit goes to `error`, Jaime opens an incident, and `get-suggestion`
reads the charm log and names the exact reset to apply. Nothing is executed
automatically - the operator applies the fix.

## Scenario

jaime-k8s is configured to watch `alertmanager-k8s` and reports `Ready` while
the workload is `active`:

```text
Model       Controller  Cloud/Region        Version  SLA          Timestamp
jaime-k8s   test        myk8scloud/default  3.6.28   unsupported  20:13:14+01:00

App             Version  Status  Scale  Charm             Channel   Rev  Address         Exposed  Message
alertmanager-k8s 0.28.0  active  1      alertmanager-k8s  2/stable  231  10.152.183.247  no
jaime-k8s               active  1      jaime-k8s                    22  10.152.183.217  no       Ready: monitoring alertmanager-k8s

Unit              Workload  Agent  Address    Ports  Message
alertmanager-k8s/0*  active  idle   10.1.0.77
jaime-k8s/0*         active  idle   10.1.0.109        Ready: monitoring alertmanager-k8s
```

Alertmanager's configuration is operator-supplied through the charm's
`config_file` option. A single malformed line is enough to break the charm's
config handling:

```bash
juju config alertmanager-k8s config_file='route: [invalid'
```

## The incident opens

The value fails YAML parsing, the `update-status` hook raises, and the unit
goes to `error`. Jaime sees the error status and starts the failure timeout:

```text
App                   Version  Status  Scale  Charm           Channel    Rev  Address         Exposed  Message
alertmanager-k8s      0.28.0   error    1     alertmanager-k8s  2/stable  231  10.152.183.247  no       hook failed: "update-status"
jaime-k8s                     active    1     jaime-k8s                    22  10.152.183.217  no       incident open: alertmanager-k8s/0 error (a09d2833)

Unit                Workload  Agent  Address    Ports  Message
alertmanager-k8s/0*  error    idle   10.1.0.77         hook failed: "update-status"
jaime-k8s/0*         active   idle   10.1.0.109        incident open: alertmanager-k8s/0 error (a09d2833)
```

## Ask for a suggestion

```bash
juju run jaime-k8s/0 -- get-suggestion
```

    Running operation 259 with 1 task
      - task 260 on unit-jaime-k8s-0

    Waiting for task 260...
    cached: "true"
    command-count: "1"
    commands: juju config alertmanager-k8s --reset config_file
    description: |-
      The unit is in error because the operator-set `config_file` value `route: [invalid` is malformed YAML. The charm's `update-status` hook fails while parsing this value with `yaml.parser.ParserError: while parsing a flow sequence`. Reset `config_file` to its default empty value so the charm can regenerate a valid Alertmanager configuration.

      ```bash
      juju config alertmanager-k8s --reset config_file
      ```
    generated-at: "2026-11-02T19:24:06.376426+00:00"
    incident-id: a09d2833-80ff-4bea-8991-7a4a774b2454
    turn-id: "1"

The diagnosis quotes the exact parser error from the charm log, identifies the
operator-supplied value as the cause, and prescribes the documented reset.

## Apply the fix

```bash
juju config alertmanager-k8s --reset config_file
```

The workload comes back as `active` on the charm's own update cycle, and Jaime
closes the incident automatically:

```text
App             Version  Status  Scale  Charm             Channel   Rev  Address         Exposed  Message
alertmanager-k8s 0.28.0  active  1      alertmanager-k8s  2/stable  231  10.152.183.247  no
jaime-k8s               active  1      jaime-k8s                    22  10.152.183.217  no       Ready: monitoring alertmanager-k8s
```

`list-incidents` keeps the whole audit trail:

```json
[
  {
    "incident_id": "a09d2833-80ff-4bea-8991-7a4a774b2454",
    "unit": "alertmanager-k8s/0",
    "workload": "error",
    "status_message": "hook failed: \"update-status\"",
    "opened_at": "2026-11-02T19:23:46.987508+00:00",
    "closed_at": "2026-11-02T19:28:30.280046+00:00",
    "report_path": "/var/log/jaime/reports/a09d2833-80ff-4bea-8991-7a4a774b2454.md",
    "status": "closed"
  }
]
```

## Takeaways

- A bad operator-supplied value can take a charm to `error` by failing its own
  hook; there is no `blocked` fanout to notice first.
- jaime-k8s detected the error status through the Juju controller API, with no
  relation to alertmanager-k8s.
- The suggestion was grounded in the actual charm log line (`ParserError:
  while parsing a flow sequence`), so the reset was unambiguous.
- The incident opened and closed on its own around the charm's update cycle -
  all recorded in `list-incidents`. For follow-up questions, use the `ask`
  action; see the [actions reference](../../reference/actions.md).