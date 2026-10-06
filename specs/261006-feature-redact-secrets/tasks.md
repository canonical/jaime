# Tasks: Redact secrets from reports

## 1. Agree the design
- [ ] [project] Review and agree `proposal.md`, `design.md` and `requirements.md`

## 2. Implement
- [ ] [python] Add `jaime-package/jaime/redact.py`: `is_sensitive_name`,
      `redact_config_value`, `redact_text`
- [ ] [python] Name-aware redaction in `report.py` `_append_section_juju_config`
      and `_append_section_summary`
- [ ] [python] Final `redact_text` pass over the assembled report in
      `generate_report`, before the file is written

## 3. Test
- [ ] [test] `tests/unit/test_redact.py`: names, patterns, marker, idempotency,
      and that ordinary values are untouched
- [ ] [test] `tests/unit/test_report.py`: a planted token in a log line, a
      config value and health output never appears; the option name does; a
      prompt built from the report contains no token
- [ ] [test] A secret-typed option renders as set/unset with no value

## 4. Document and close
- [ ] [docs] Replace the "no redaction anywhere" paragraph in `ARCHITECTURE.md`,
      add `redact.py` to the module list and record the policy
- [ ] [docs] `docs/config.md`: note that sensitive option values are redacted
- [ ] [project] Regenerate examples if the report changes (`make examples`)
- [ ] [project] On merge: set `Status: done` in `proposal.md` and update the
      descriptive sections of `ARCHITECTURE.md`, `TASKS.md` 4.9 and `CHANGELOG.md`
