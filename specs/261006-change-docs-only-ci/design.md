# Design: Docs-only changes run only the docs checks

## Decision 1: per-job gating, not workflow path filters

Add a `changes` job that computes a single `code` boolean, then gate jobs with
`if:`:

- `docs` always runs.
- `lint` and `unit` run when `code` is true.
- `pack` already needs `lint`, `docs` and `unit`; it runs when `code` is true,
  and is skipped when its needs are skipped.
- `integration` needs `pack` and is skipped transitively.

Rejected: workflow-level `paths:` / `paths-ignore:`. A workflow skipped entirely
by a path filter leaves required status checks at "Expected - Waiting", which
blocks merges. A per-job `if:` emits a "skipped" check, which satisfies required
checks.

## Decision 2: what counts as docs-only

Docs-only:

- `**/*.md`
- `docs/**`
- `specs/**`
- `.mdformat.toml`

Everything else is code. Notably `.github/**`, `examples/**`, `pyproject.toml`,
`tox.ini`, `Makefile`, `charms/**`, `jaime-package/**`, `tests/**`,
`scripts/**`. Treating `.github/**` as code matters: a workflow edit must
exercise the workflow. `examples/**` is generated from code, and treating it as
code keeps the `make examples` drift test in play.

## Decision 3: detection

Two options, undecided.

- **Custom shell step (preferred).** Checkout with `fetch-depth: 0`. For
  `pull_request`, diff `HEAD` against the merge base with the base ref; for
  `push`, diff against `github.event.before` with a fallback for a new or forced
  branch. Classify each path against the allowlist. No new dependency.
- **`dorny/paths-filter`, pinned by SHA.** Declarative filters, and it handles
  the pull-request merge base and push events. Renovate would manage the pin, as
  it does `charmed-kubernetes/actions-operator`. Cost: one more third-party
  action in the supply chain.

## Caveats

- The `changes` job adds roughly 10-20 seconds to every run.
- A mixed change (docs and code) runs the full pipeline, including the docs
  checks. Correct.
- A force push or a new branch needs the shell fallback; `dorny/paths-filter`
  handles these itself.
- **Skipped `unit` is a gap for the planned docs-consistency test.** That test
  is a `tests/unit` test, so a docs-only change that breaks it would skip it.
  The `docs` job should run that test directly once it exists.

## Open question: mdformat scope

`mdformat` currently checks only `README.md` and `docs/`. Under this change, a
docs-only pull request that adds a spec or edits `TASKS.md` gets spelling checked
(codespell is repo-wide) but not formatting. Enforcing formatting for specs and
tasks requires a one-time reformat:

| Scope | Churn |
|---|---|
| `README.md` + `docs/` (today) | none |
| add `specs/` | ~83 lines |
| add `specs/` + root Markdown | ~371 lines (`TASKS.md` 130, `AGENTS.md` 70, `ARCHITECTURE.md` 40, `CONTRIBUTING.md` 22, `CHANGELOG.md` 2, `specs/**` ~83) |

The reformat changes list spacing and continuation indents in agent-facing
files. Suggested: add `specs/` at least, since specs are copied by agents and
consistency matters; leave the root agent files as they are.

## Open question: required checks

If `main` has required status checks, per-job `if:` is safe (skipped checks
satisfy them). This should be confirmed before implementing.
