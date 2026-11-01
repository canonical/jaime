# MySQL group replication port blocked

This is a real incident handled end to end with the machine subordinate charm
(`jaime`) and a three-member MySQL InnoDB cluster (`mysql-innodb-cluster`),
running in `suggest` mode. A single firewall rule cuts one member off from the
Group Replication communication port; the charm goes `blocked`, Jaime opens an
incident and `get-suggestion` pinpoints the exact rule to remove. Nothing is
executed automatically - the operator applies the fix.

## Scenario

A healthy three-member cluster, with one primary (`/3`) and two secondaries
(`/4`, `/5`):

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
postgresql  test  localhost/localhost  3.6.28   unsupported  08:54:14+01:00

App                   Version  Status  Scale  Charm                 Channel     Rev  Exposed  Message
jaime                          active      3  jaime                               2  no       Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster  8.0.46   active      3  mysql-innodb-cluster  8.0/stable  179  no       Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.

Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/3*  active    idle   6        10.82.123.185          Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/7                active    idle            10.82.123.185          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/4   active    idle   7        10.82.123.146          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/5*               active    idle            10.82.123.146          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/5   active    idle   8        10.82.123.128          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/6                active    idle            10.82.123.128          Ready: monitoring mysql-innodb-cluster
```

Group Replication members talk to each other on the GCS port (`33061`). Dropping
inbound traffic to that port on one member's host isolates it from the group
without stopping its local MySQL process:

```bash
juju ssh mysql-innodb-cluster/5 -- sudo iptables -A INPUT -p tcp --dport 33061 -j DROP
```

## The incident opens

Within minutes the isolated member reports `blocked`, and the co-located Jaime
unit switches to `maintenance` while it collects context:

```text
Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/3*  active    idle   6        10.82.123.185          Unit is ready: Mode: R/W, Cluster is NOT tolerant to any failures. 1 member is not active.
  jaime/7                active    idle            10.82.123.185          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/4   active    idle   7        10.82.123.146          Unit is ready: Mode: R/O, Cluster is NOT tolerant to any failures. 1 member is not active.
  jaime/5*               active    idle            10.82.123.146          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/5   blocked   idle   8        10.82.123.128          Cluster is inaccessible from this instance. Please check logs for details.
  jaime/6                maintenance    idle            10.82.123.128          collecting context: mysql-innodb-cluster/5 (0f0bfccb)
```

Once collection finishes, the incident is open and recorded in Jaime's status:

```text
Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/3*  active    idle   6        10.82.123.185          Unit is ready: Mode: R/W, Cluster is NOT tolerant to any failures. 1 member is not active.
  jaime/7                active    idle            10.82.123.185          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/4   active    idle   7        10.82.123.146          Unit is ready: Mode: R/O, Cluster is NOT tolerant to any failures. 1 member is not active.
  jaime/5*               active    idle            10.82.123.146          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/5   blocked   idle   8        10.82.123.128          Cluster is inaccessible from this instance. Please check logs for details.
  jaime/6                active    idle            10.82.123.128          incident open: mysql-innodb-cluster/5 blocked (0f0bfccb)
```

Because the block and the incident live on the **same host**, the collector has
direct access to the evidence: the MySQL error log shows the GCS connectivity
failure, and the firewall rules show the `DROP`.

## Ask for a suggestion

```bash
juju run jaime/6 -- get-suggestion
```

    Running operation 19 with 1 task
      - task 20 on unit-jaime-6

    Waiting for task 20...
    cached: "true"
    command-count: "1"
    commands: juju ssh mysql-innodb-cluster/5 -- sudo iptables -D INPUT -p tcp --dport
      33061 -j DROP && juju run mysql-innodb-cluster/5 restart-services
    description: |-
      Root cause: the unit was expelled from the InnoDB cluster and auto-rejoin failed because nftables/iptables is dropping TCP `33061`, the Group Replication GCS port. The MySQL error log explicitly shows `[GCS] failed to test connectivity to the local group communication engine on 10.82.123.128:33061`, and the firewall rules confirm a `DROP` on `dport 33061`. Per the charm docs (https://discourse.charmhub.io/t/mysql-innodb-cluster-docs-index/11009), this port must be open for cluster communication. Remove the drop rule and restart the MySQL service so the node can rejoin.

      ```bash
      juju ssh mysql-innodb-cluster/5 -- sudo iptables -D INPUT -p tcp --dport 33061 -j DROP && juju run mysql-innodb-cluster/5 restart-services
      ```
    generated-at: "2026-11-01T07:57:24.975102+00:00"
    incident-id: 0f0bfccb-c4a9-44b1-bdba-46d875f52414
    turn-id: "1"

The diagnosis names the port, quotes the actual error-log evidence, and explains
why the firewall rule breaks membership. The suggested command is the complete
fix: remove the rule **and** restart the MySQL service so the member can rejoin.

## Apply the fix

```bash
juju ssh mysql-innodb-cluster/5 -- sudo iptables -D INPUT -p tcp --dport 33061 -j DROP && juju run mysql-innodb-cluster/5 restart-services
```

## Verify the recovery

The cluster returns to full tolerance and every unit ends `active` again:

```text
Model  Controller  Cloud/Region         Version  SLA          Timestamp
postgresql  test  localhost/localhost  3.6.28   unsupported  09:02:47+01:00

App                   Version  Status  Scale  Charm                 Channel     Rev  Exposed  Message
jaime                          active      3  jaime                               2  no       Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster  8.0.46   active      3  mysql-innodb-cluster  8.0/stable  179  no       Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.

Unit                     Workload  Agent  Machine  Public address  Ports  Message
mysql-innodb-cluster/3*  active    idle   6        10.82.123.185          Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/7                active    idle            10.82.123.185          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/4   active    idle   7        10.82.123.146          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/5*               active    idle            10.82.123.146          Ready: monitoring mysql-innodb-cluster
mysql-innodb-cluster/5   active    idle   8        10.82.123.128          Unit is ready: Mode: R/O, Cluster is ONLINE and can tolerate up to ONE failure.
  jaime/6                active    idle            10.82.123.128          Ready: monitoring mysql-innodb-cluster
```

## Takeaways

- A silent network rule can take out a cluster member while the local process
  stays up - the `blocked` status is the only visible symptom.
- The incident opened on the host that owns the fault, so the collected context
  contained the decisive evidence (the `[GCS] failed to test connectivity` log
  line and the firewall `DROP` rule) and the suggestion was exact.
- The fix was one operator command that restores the rule and restarts the
  service - nothing was executed automatically.
- For follow-up questions on the same incident, use the `ask` action instead of
  re-running `get-suggestion`; see the
  [actions reference](../../reference/actions.md).