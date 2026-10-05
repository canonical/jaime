# Specs

A spec records one agreed change before it is implemented. `ARCHITECTURE.md`
remains the source of truth for current behaviour and direction; this folder
holds the detail of a change while it is being made.

## When to write one

Write a spec when a task changes behaviour, an interface, a config option or a
stated boundary. That is the same trigger as the `ARCHITECTURE.md`-first rule in
`AGENTS.md`. Small fixes, docs and routine work go straight from `TASKS.md`.

`TASKS.md` owns phase order and the release milestone and links to the spec. The
spec owns the detail and holds the only checklist; do not duplicate it in
`TASKS.md`.

## Layout

    specs/YYMMDD-type-short-description/
      proposal.md      why, what changes, what does not, impact
      design.md        decisions, rejected alternatives, cost, bounds, edge cases
      requirements.md  requirements with WHEN/THEN scenarios
      tasks.md         checklist with owner agents

- Date: `YYMMDD` is the day the spec is created; the folder is never renamed
  afterwards.
- Type: a commit type from `AGENTS.md` (`feature`, `fix`, `change`, `docs`).
- Copy `_template/` to start a new spec.

## Lifecycle

1. Propose: open a pull request containing only the spec folder, plus any
   `ARCHITECTURE.md` direction change. Get it agreed.
2. Implement per `tasks.md`. Commits use the spec's type.
3. Close: set `Status: done` in `proposal.md`, and update the descriptive
   sections of `ARCHITECTURE.md`, `TASKS.md` and `CHANGELOG.md`. The folder
   stays in place as the decision record; there is no archive directory.