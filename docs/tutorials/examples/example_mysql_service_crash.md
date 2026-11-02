# MySQL mysqld service crash (machine)

This is a real incident handled end to end with the machine subordinate charm
(`jaime`) and a three-member MySQL InnoDB cluster (`mysql-innodb-cluster`),
running in `suggest` mode. The workload service on one member crashes and enters
the systemd `failed` state; the charm goes `blocked`, Jaime opens an incident,
and `get-suggestion` reads the journal evidence and names the documented
recovery action. Nothing is executed automatically - the operator applies the
fix.

## Scenario

A healthy three-member cluster, with one primary (`/6`) and two secondaries
(`/7`, `/8`):

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
postgresql  test  localhost/localhost  3.6.28   unsupported  19:33:39+01:00

App                   Version  Status  Scale  Charm                 Channel     Rev  Exposed  Message
jaime                          active      3  jaime                               2  no       Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster  8.0.46   active      3  mysql-innodb-cluster  8.0/stable  179  no       Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.

Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/6*  active    idle   9        10.82.123.218          Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/10               active    idle            10.82.123.218          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/7   active    idle   10       10.82.123.17           Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/8*               active    idle            10.82.123.17           Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/8   active    idle   11       10.82.123.211          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/9                active    idle            10.82.123.211          Ready: monitoring mysql-innodb-cluster
```

The workload runs `mysql.service`, which has `Restart=on-failure` with a start
limit: five failed starts in 10 seconds stop the retries and leave the unit
`failed`. A single `SIGKILL` therefore only causes churn - it takes a short
crash loop to break the service for real:

```bash
juju ssh mysql-innodb-cluster/8 -- 'for i in $(seq 1 6); do sudo systemctl kill -s KILL mysql.service; sleep 0.4; done'
```

systemd now reports:

```text
● mysql.service - MySQL Community Server
     Loaded: loaded (/usr/lib/systemd/system/mysql.service; enabled; preset: enabled)
     Active: failed (Result: signal) since Mon 2026-11-02 18:34:24 UTC; 130ms ago
    Process: 119656 ExecStartPre=/usr/share/mysql/mysql-systemd-start pre (code=exited, status=0/SUCCESS)
    Process: 119665 ExecStart=/usr/sbin/mysqld (code=killed, signal=KILL)
```

## The incident opens

The unit reports `blocked`, and the co-located Jaime unit first stands by
(`waiting`) while the timeout elapses, then switches to `maintenance` while it
collects context:

```text
mysql-innodb-cluster/8  blocked   idle   11       10.82.123.211          Cluster is inaccessible from this instance. Please check logs for details.
  jaime/9               waiting   idle            10.82.123.211          blocked - waiting (0.0/1 min)
...
mysql-innodb-cluster/8  blocked      idle   11       10.82.123.211          Services not running that should be: mysql
  jaime/9               maintenance  idle            10.82.123.211          collecting context: mysql-innodb-cluster/8 (e6a70aaf)
```

Once collection finishes the charm's health check pins the real message
(`Services not running that should be: mysql`) and the incident is recorded in
Jaime's status:

```text
mysql-innodb-cluster/8  blocked   idle   11       10.82.123.211          Services not running that should be: mysql
  jaime/9               active    idle            10.82.123.211          incident open: mysql-innodb-cluster/8 blocked (e6a70aaf)
```

## Ask for a suggestion

```bash
juju run jaime/9 -- get-suggestion
```

````
Running operation 23 with 1 task
  - task 24 on unit-jaime-9

Waiting for task 24...
cached: "true"
command-count: "1"
commands: juju run mysql-innodb-cluster/8 restart-services
description: |-
  The unit is blocked because `mysqld` is not running: `mysql.service` is in a failed state after repeated start attempts, and ports `3306`, `33060`, and `33061` are not listening. The MySQL error log shows Group Replication failing to join peers, preventing the service from becoming healthy. The `LC_ALL` locale errors are affecting `mysqlsh` health checks, but the immediate blocker is the down MySQL service. Per the charm docs linked in the report, `restart-services` is the charm action for starting the managed services.

  ```bash
  juju run mysql-innodb-cluster/8 restart-services
  ```
generated-at: "2026-11-02T18:36:18.272209+00:00"
incident-id: e6a70aaf-6880-4ecb-8087-142d98e59b12
turn-id: "1"
````

The diagnosis reads the failed state, the missing listeners and the Group
Replication log, separates the real blocker (the down service) from the noise
(`LC_ALL` locale warnings), and points at the charm's own managed action rather
than a raw `systemctl` call.

## Apply the fix

```bash
juju run mysql-innodb-cluster/8 restart-services
```

## Verify the recovery

The service comes back, the member rejoins the group, and the unit returns to
the full-tolerance state:

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
postgresql  test  localhost/localhost  3.6.28   unsupported  19:37:33+01:00

App                   Version  Status  Scale  Charm                 Channel     Rev  Exposed  Message
jaime                          active      3  jaime                               2  no       Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster  8.0.46   active      3  mysql-innodb-cluster  8.0/stable  179  no       Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.

Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/6*  active    idle   9        10.82.123.218          Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/10               active    idle            10.82.123.218          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/7   active    idle   10       10.82.123.17           Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/8*               active    idle            10.82.123.17           Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/8   active    idle   11       10.82.123.211          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/9                active    idle            10.82.123.211          Ready: monitoring mysql-innodb-cluster
```

`list-incidents` shows the whole lifecycle: the incident opened two minutes
later closed itself on recovery, with the report path and the status message
captured at open time:

```json
[
  {
    "incident_id": "e6a70aaf-6880-4ecb-8087-142d98e59b12",
    "unit": "mysql-innodb-cluster/8",
    "workload": "blocked",
    "status_message": "Services not running that should be: mysql",
    "first_seen": "2026-11-02T18:34:17+00:00",
    "status_since": "2026-11-02T18:34:46+00:00",
    "opened_at": "2026-11-02T18:35:19.755732+00:00",
    "report_path": "/var/log/jaime/reports/e6a70aaf-6880-4ecb-8087-142d98e59b12.md",
    "closed_at": "2026-11-02T18:37:33.444992+00:00",
    "status": "closed"
  }
]
```

## Takeaways

- A crash-looping service ends in the systemd `failed` state; a single kill
  only causes churn, because `Restart=on-failure` brings it back.
- Jaime's co-located collector had the decisive evidence on the same host: the
  failed unit, the missing listeners and the Group Replication logs - which is
  why the suggestion was exact and separated the real cause from noise.
- The recovery used the charm's managed `restart-services` action, not a raw
  `systemctl` call.
- Every incident is auditable: `list-incidents` correlates when it opened and
  closed, the status message at open, and the stored report path.
- For follow-up questions on the incident, use the `ask` action; see the
  [actions reference](../../reference/actions.md).
