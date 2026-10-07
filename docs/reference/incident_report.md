# Incident report

Every incident produces a **Markdown report** written under `report-dir`
(default `/var/log/jaime/reports/<incident-id>.md`). Reports are the persisted
evidence artifact: they are produced from collected facts alone, never from
model output, and are the input the AI receives in `suggest` mode.

Signals are ordered so the most useful content comes first:

- a header with the workload **status message** (the "reason" Juju reports)
- an executive summary with that message, the most recent error/warning log
  lines, and the operator-changed configuration
- host or pod evidence: logs, processes, systemd, network, disk, memory; on
  Kubernetes, the pod and container state
- the charm's **available actions** and **documentation links** (machine
  charm), so a suggested fix matches what the workload actually exposes

Sections with no data are omitted entirely.

Reports redact sensitive config values and recognisable secrets before they
are written, so the persisted report and the prompt built from it contain
none. The audit log and `status-state.json` are **not** redacted, so do not
put secrets in a workload status message.

See `examples/report.md` in the repository for the generated shape.
