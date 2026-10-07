# Proposal: Docs-only changes run only the docs checks

Type: change
Status: proposed

Tracked as an idea in `TASKS.md`; not part of the 0.1.0 release.

## Why

CI runs lint, the three unit suites, charm packing and the machine integration
suite on every pull request. A change that only touches Markdown (the README,
`docs/`, `specs/`, `TASKS.md`, `ARCHITECTURE.md`) cannot affect the code suites,
yet it pays for the full run, including the slow LXD pack and integration jobs.
Docs and specs changes are common, so the cost adds up.

## What changes

- Detect whether a change touches any non-documentation file.
- Run only the docs checks when it does not: `codespell`, `mdformat --check`,
  and (once it exists) the docs-consistency test.
- Run the full pipeline when it does.

## Not changing

- The checks themselves, or their current scope. Whether `mdformat` should cover
  `specs/` and the root Markdown is a separate decision, recorded in
  `design.md`.
- Branch protection or required checks.
- The release pipeline.
