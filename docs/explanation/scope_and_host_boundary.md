# Scope and host boundary

The two Jaime variants differ in what they can see, and that difference is
deliberate.

## Machine charm

A machine subordinate monitors only units on **its own host**. Its collectors
read the local machine — unit logs, `/var/lib/juju/agents`, `df`, `free`,
`ps`, `ss`, systemd, firewall — so a report about a unit on another machine
would carry this host's diagnostics. That would be misleading, so Jaime does
not offer it. Cover more machines by relating Jaime to more principals.

If a machine hosts several principal units and Jaime is related to more than
one of them, Juju places a Jaime unit alongside each. With
`watch-applications` set, those units would monitor the same host and report
the same fault twice. Jaime flags this in its unit status; relate it to one
principal per machine to avoid it.

## Kubernetes charm

The Kubernetes charm has no such limit: it runs as its own pod and reads any
application in the model's namespace through the Kubernetes API.

## Status messages

On Kubernetes the workload status (and its message) always comes from the
controller API. On the machine charm the principal's status is normally read
from the local `goal-state` hook tool, which carries the status name and
timestamp but **not the message**; granting the observer captures the message,
and is what enables watching co-located units.
