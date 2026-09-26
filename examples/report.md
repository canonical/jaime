# Incident Report
- incident: e7a3f1b2-8c4d-4a5e-9b6f-2c1d3e4f5a6b
- unit: postgresql/0
- status: blocked
- first-seen: 2026-07-14T09:55:00+00:00
- generated: 2026-07-14T10:05:00+00:00

## Executive summary
Unit is in **blocked** state.

**Recent errors/warnings:**
```
2026-07-14 09:55:01 WARNING unit.postgresql/0.juju-log Cannot start: no replicas
2026-07-14 09:56:10 ERROR unit.postgresql/0.juju-log snap service charmed-postgresql.patroni is failed
2026-07-14 09:57:00 ERROR unit.postgresql/0.juju-log Hook start failed: disk full
```

**Charm options with non-empty schema defaults:**
- `max_connections`: `100`
- `port`: `5432`

## Network ports

- `5432/tcp` → not_listening ✗

- `6432/tcp` → listening ✓

## Network connections (listening + active)
```
tcp LISTEN 0 128 0.0.0.0:6432 0.0.0.0:* users:(("pgbouncer",pid=1234,fd=7))
tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=800,fd=3))
```

## Firewall rules (iptables — IPv4)
```
Chain INPUT (policy DROP)
target     prot opt source               destination
ACCEPT     tcp  --  0.0.0.0/0            0.0.0.0/0            tcp dpt:6432
```

## Firewall rules (ufw)
```
Status: active
6432/tcp                   ALLOW       Anywhere
```

## Log files

- /var/log/postgresql/postgresql-16-main.log (high) — _Main PostgreSQL log_ ✓

```
2026-07-14 09:55:01 UTC [12345] LOG:  received SIGHUP, reloading configuration files
2026-07-14 09:56:00 UTC [12346] WARNING:  database "app" has no active replicas
2026-07-14 09:57:30 UTC [12347] ERROR:  out of disk space on primary
```

- /var/log/postgresql/postgresql-16-main.log.1 (medium) — _Rotated PostgreSQL log (recent)_ ✓

```
2026-07-14 09:50:00 UTC [12340] LOG:  checkpoint starting: time
2026-07-14 09:52:00 UTC [12341] LOG:  checkpoint complete: wrote 42 buffers
```

- /var/log/postgresql/pg_stat_statements.log (low) — _Query statistics log_ ✗ (not_found)

## Processes

- **postgres**: 0 running (expected 1-4) ✗

- **pgbouncer**: 1 running (expected 1-1) ✓

## Systemd units

- `postgresql@16-main.service` → failed (dead, restarts=3, exec=1) ✗

- `postgresql-exporter.service` → active (running) ✓

## Environment variables

_Only whether each variable is set; values are never collected._

- `PGDATA` — set ✓

- `PGPORT` — set ✓

- `POSTGRES_PASSWORD` — unset ✗

## Health commands

- `$ systemctl is-active postgresql@16-main.service` → exit 1 ✗

  ```
inactive
  ```

- `$ pg_isready -h localhost -p 5432` → exit 2 ✗

  ```
/tmp:5432 - no response
  ```

## Snap packages

```
Name                  Version   Rev    Tracking       Publisher   Notes
charmed-postgresql    16.4      123    16/stable      canonical✓  -
core22                20240920  1622   latest/stable  canonical✓  base
```

## Snap services
```
Name                             Startup   Current   Notes
charmed-postgresql.patroni       enabled   failed    -
charmed-postgresql.pgbackrest    enabled   active    -
```

## Failed snap changes
```
42  Error  today at 09:56  today at 09:56  Start service charmed-postgresql.patroni
```

## Snap logs: `charmed-postgresql` (failed)
```
09:55:58 patroni[4321]: INFO: no action. I am (postgresql-0), the leader
09:56:04 patroni[4321]: ERROR: could not connect to local PostgreSQL
09:56:04 patroni[4321]: ERROR: Exiting
09:56:05 systemd[1]: charmed-postgresql.patroni.service: Failed with result 'exit-code'
```

## Charm config

- `max_connections`: `100`

- `port`: `5432`

## Disk usage

```
Filesystem     Size  Used Avail Use% Mounted on
/dev/sda1       20G   19G  1.0G  95% /
```

## Memory

RAM: 6.1G used / 7.7G total (1.2G available)
Swap: 0.5G used / 2.0G total

## Recent unit logs

_Showing only lines matching `error` or `warning` (case-insensitive), with a context window around the last match._

_Logs are in chronological order._

```
2026-07-14 09:55:01 WARNING unit.postgresql/0.juju-log Cannot start: no replicas
2026-07-14 09:56:10 ERROR unit.postgresql/0.juju-log snap service charmed-postgresql.patroni is failed
2026-07-14 09:57:00 ERROR unit.postgresql/0.juju-log Hook start failed: disk full
2026-07-14 09:58:00 INFO juju.worker Running cleanup hooks
```
