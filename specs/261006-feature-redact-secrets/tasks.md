# Tasks: Redact secrets from reports

## 1. Agree the design
- [x] [project] Review and agree `proposal.md`, `design.md` and `requirements.md`

## 2. Implement
- [x] [python] Add `jaime-package/jaime/redact.py`: `is_sensitive_name`,
      `redact_config_value`, `redact_text`
- [x] [python] Name-aware redaction in `report.py` `_append_section_juju_config`
      and `_append_section_summary`
- [x] [python] Final `redact_text` pass over the assembled report in
      `generate_report`, before the file is written

## 3. Test
- [x] [test] `tests/unit/test_redact.py`: names, patterns, marker, idempotency,
      and that ordinary values are untouched
- [x] [test] `tests/unit/test_report.py`: a planted token in a log line, a
      config value and health output never appears; the option name does; a
      prompt built from the report contains no token
- [x] [test] A secret-typed option renders as set/unset with no value
- [x] [test] `generate-report` and `get-suggestion` paths: the regenerated report
      and the prompt built from it contain no planted secret (covered by
      rendering through the single writer both paths use)

## 4. Document and close
- [x] [docs] Replace the "no redaction anywhere" paragraph in `ARCHITECTURE.md`,
      add `redact.py` to the module list and record the policy
- [x] [docs] `docs/config.md`: note that sensitive option values are redacted
- [x] [project] Regenerate examples if the report changes (`make examples`)
      (no change: the example carries no secrets)
- [ ] [project] On merge: set `Status: done` in `proposal.md` and update the
      descriptive sections of `ARCHITECTURE.md`, `TASKS.md` 4.9 and `CHANGELOG.md`
