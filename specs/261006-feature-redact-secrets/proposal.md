# Proposal: Redact secrets from reports

Type: feature
Status: proposed

Tracks `TASKS.md` 4.9. Gates the 0.1.0 CharmHub release (5.3).

## Why

The Kubernetes report renders every config option value of the watched
application, in `_append_section_juju_config` and again in the executive
summary. Collected logs and health-command output can carry tokens, and the
suggest prompt embeds the report verbatim, so a secret in the report reaches the
model. `AGENTS.md` requires secrets never to reach logs, reports or prompts, and
collecting more evidence (4.5) made the problem worse.

## What changes

- A shared redaction module (`jaime-package/jaime/redact.py`) applied when the
  report is written.
- Config options whose name is sensitive, or whose Juju type is `secret`, are
  rendered as `[REDACTED]` (or set/unset) instead of their value, with the
  option name kept so the report stays diagnostic.
- A conservative value-pattern scrub over the assembled report removes
  recognisable secrets from logs, health-command output, snap logs and the
  status message.
- The prompt is clean because it is built by reading the redacted report back.

## Not changing

- Collection. Context is redacted at render time, not captured redacted, and the
  in-memory context is never persisted.
- The audit log and `status-state.json`. A token embedded in a workload status
  message can still reach them; see `design.md`, Non-goals.
- Operator-supplied `additional-context`, which is trusted and passed through.
- Environment variables, already names and set/unset only.
- No reversible redaction, and no entropy-based guessing that would mangle IDs
  and hashes.

## Impact

- `jaime-package/jaime/redact.py` (new)
- `jaime-package/jaime/report.py` (two config renderers plus a final scrub)
- `tests/unit/test_redact.py` (new), `tests/unit/test_report.py`
- `ARCHITECTURE.md`, `docs/config.md`
