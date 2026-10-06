# Tasks: Docs-only changes run only the docs checks

## 1. Agree the design
- [ ] [project] Decide the detection approach: custom shell step or `dorny/paths-filter` (see `design.md`)
- [ ] [project] Confirm whether `main` has required status checks
- [ ] [project] Decide the `mdformat` scope: `README.md` + `docs/` only, add `specs/`, or all Markdown

## 2. Implement
- [ ] [test] Add a `changes` job that outputs a `code` boolean, classifying paths per `design.md`
- [ ] [test] Gate `lint` and `unit` on `code`; keep `docs` always running
- [ ] [test] Gate `pack` and `integration` on `code` so they skip for docs-only changes
- [ ] [test] If the docs-consistency test exists, run it from the `docs` job so docs-only changes still validate docs

## 3. Test and document
- [ ] [test] Verify a docs-only pull request runs only the docs job
- [ ] [test] Verify a code change runs the full pipeline
- [ ] [docs] Note the docs-only behaviour in `CONTRIBUTING.md`
- [ ] [project] On merge: set `Status: done` in `proposal.md`, move this entry out of the `TASKS.md` Ideas section, and update `CHANGELOG.md`
