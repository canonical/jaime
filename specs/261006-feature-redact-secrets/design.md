# Design: Redact secrets from reports

## Decision 1: redact when the report is written

The report is the persisted artifact and the source of the prompt: `core.py`
reads the report file back before calling the provider, so one render-time pass
cleans both. Rejected:

- **Redact in each collector.** Many call sites, easy to miss a future section,
  and the prompt path would still need its own pass.
- **Redact only the prompt.** Leaves the persisted report on disk with secrets.

Both report-producing and prompt-consuming paths go through this point. The
`generate-report` action re-collects context and calls `generate_report`, so the
regenerated file is redacted; `get-suggestion`, and the incident path, read that
file back before building the prompt. A cached suggestion is returned without a
provider call and was generated from a redacted report.

Reports written before this change, and suggestions cached before it, are not
retroactively scrubbed. They are replaced on the next `generate-report` or new
incident.

## Decision 2: two layers

- **Structured, name-aware redaction** for config values. A secret with no
  recognisable shape (a random passphrase) is invisible to a pattern scan, so
  the option name and Juju type decide.
- **A conservative value-pattern scrub** over the assembled report text, for
  logs, health-command output, snap logs and the status message.

## Decision 3: option names and types

Normalise the option name: lowercase, split camelCase and separators, and also
test the separators-removed form. Treat it as sensitive when it contains one of
`password`, `passwd`, `secret`, `token`, `credential`, or the joined forms
`apikey`, `privatekey`, `secretkey`, `accesskey`. This avoids naive substring
false positives such as `monkey`, `keyboard` and `author`.

An option Juju reports with `type: secret`, or whose value is a `secret:` URI, is
rendered as set/unset and never by value.

For a sensitive name the option name stays visible and only the value is
replaced with `[REDACTED]`, so the report keeps its diagnostic shape.

## Decision 4: value patterns (conservative)

- Juju secret URI: `secret:<id>`
- `Bearer <token>`, case-insensitive
- PEM private key blocks (`-----BEGIN ... PRIVATE KEY-----`)
- JWT: `eyJ...` with three dot-separated segments
- AWS access key id: `AKIA` plus 16 uppercase alphanumerics
- `key: value` or `key=value` assignments whose key is sensitive

No generic high-entropy guessing: it would redact commit SHAs, UUIDs and IDs and
destroy evidence.

## Decision 5: marker and idempotency

Substitutions use `[REDACTED]`. Redaction is idempotent: a second pass over
already-redacted output is byte-identical. Ordinary values (IDs, versions, paths,
dates) are unchanged.

## Bounds and safety

- Redaction runs after collection caps, over bounded text, so it adds no
  performance or memory concern.
- The report keeps its section structure and option names; only values change.
- Non-string config values are coerced safely.
- The in-memory context is never persisted, so redacting at render time is
  sufficient for the stated scope.

## Edge cases

| Case | Behaviour |
|---|---|
| sensitive name, shapeless value | value replaced by `[REDACTED]` |
| `type: secret` option | set/unset only |
| a log line that is entirely a secret | the secret is replaced by `[REDACTED]` |
| multi-line PEM in a log | block replaced by one marker |
| value already `[REDACTED]` | unchanged (idempotent) |
| ordinary UUID / SHA / path / version | unchanged |
| secret in both the summary and the full config section | both redacted |
| a report written before this change | not retroactively scrubbed; replaced on the next `generate-report` or new incident |
| a suggestion cached before this change | returned as-is; regenerated when the model or context hash changes |

## Non-goals

- The audit log (`events.jsonl`) and `status-state.json`. `status_message` is
  redacted only where it appears in the report, so a token embedded in a
  workload status message can still reach those stores. Closing that needs
  capture-time redaction and belongs to a later change.
- Operator-supplied `additional-context`, which is trusted.
- Reversible redaction or encryption.

## Open implementation check

Confirm how `Application.Get` exposes a secret-backed config option: whether the
entry carries `type: secret`, whether `value` is a `secret:<id>` URI, or both.
Decision 3 handles either, but the implementation should not assume one and the
unit test fixture should match the real shape. This needs a live controller to
settle.
