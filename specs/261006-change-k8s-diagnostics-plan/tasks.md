# Tasks: Kubernetes diagnostics plan

## 1. Agree the design
- [x] [project] Review and agree `proposal.md`, `design.md` and `requirements.md`
- [x] [project] Reword `ARCHITECTURE.md` Phase 4 from "parity" to a k8s-scoped plan

## 2. Implement
- [x] [python] Add `validate_k8s_diagnostics` to `jaime-package/jaime/diagnostics.py`:
      schema, caps, regex compile check
- [x] [charm] Add `diagnostics` to `charms/k8s/config.yaml`; validate on
      config-changed; block with the first error and write a JSONL error event
- [x] [charm] Pass the application's plan to k8s `collect_context` from both
      `_collect_incident_context` and `_collect_report_context`
- [x] [python] Honour `containers`, `log_patterns`, `env_variables` and `ports`
      in `charms/k8s/src/jaime/collector.py`; emit `plan_results`
- [x] [python] Render k8s plan results in the report: reuse the env section,
      accept the `declared` port status in `_append_section_network`, add a
      plan-containers section (see `design.md`, Decision 4)

## 3. Test
- [x] [test] Validator: valid, invalid JSON, wrong types, over-cap, bad regex
- [x] [test] Collector: no plan is unchanged; container selection; missing
      container; log patterns within caps; env value never present; port missing
- [x] [test] Charm: invalid plan blocks; valid plan clears the block
- [x] [test] Report: declared port wording; a missing plan container renders as
      not found
- [x] [test] Add `diagnostics` to `_SUBSTRATE_SPECIFIC` in
      `tests/unit/test_config_consistency.py`, with a stated reason

## 4. Document and close
- [x] [docs] `docs/config.md`: k8s `diagnostics` reference with an example
- [x] [project] Regenerate examples if the report changes (`make examples`)
      (no change: the example carries no k8s plan)
- [ ] [project] On merge: set `Status: done` in `proposal.md` and update the
      descriptive sections of `ARCHITECTURE.md`, `TASKS.md` 4.6 and `CHANGELOG.md`