# Contributing to Jaime

Jaime is a Juju diagnostic and incident reporting charm, shipped in two
variants: a **machine subordinate** (`charms/machine/`) and a **Kubernetes
standalone** (`charms/k8s/`). Both share the substrate-agnostic library in
`jaime-package/`.

## Before you start

- **`ARCHITECTURE.md`** is the roadmap and the source of truth for direction.
- **`TASKS.md`** is the currently active implementation plan.
- **`AGENTS.md`** defines the engineering conventions this project follows.

Where these conflict, `ARCHITECTURE.md` wins.

If a change is not covered by an open item in `TASKS.md`, open an issue first so
scope can be agreed before you write code.

See `docs/index.md` for the documentation index. The `docs/` tree follows the
Diataxis structure (tutorials, how-to guides, reference, explanation); the
how-to guides cover build and test instructions, and the tutorials cover
deployment.

## Legal

By contributing you agree that your work is licensed under the
[Apache License 2.0](LICENSE), the licence covering this repository.

Canonical requires contributors to sign the
[Canonical contributor licence agreement](https://ubuntu.com/legal/contributors)
before their first contribution can be merged. It is a one-off step covering
all Canonical projects.

We follow the [Ubuntu Code of Conduct](https://ubuntu.com/community/ethos/code-of-conduct).

## Opening issues

Open issues at https://github.com/canonical/jaime/issues. For bugs, include the
charm variant (machine or k8s), the Juju version, the charm revision and the
relevant `juju debug-log` output — redacted, since logs and reports may contain
detail from your environment.

## Commits

Commit messages follow:

```text
[agent] type: description
```

`agent` is the area of ownership: `project`, `charm`, `python`, `test`,
`security` or `docs`. See `AGENTS.md` for what each covers.

`type` is one of:

| Type      | Use for                                                       |
| --------- | ------------------------------------------------------------- |
| `feature` | new functionality                                             |
| `fix`     | defect fix                                                    |
| `change`  | refactoring, architecture, configuration, non-functional work |
| `docs`    | documentation-only changes                                    |

For example:

```text
[charm] feature: Add subordinate charm skeleton
[python] fix: Fix cooldown logic
[docs] docs: Add configuration reference
```

Write descriptions in the imperative, short and factual. Explain *why* in the
body when the reason is not obvious from the diff.

## Pull requests

- Keep changes surgical. Change only what the task requires; avoid unrelated
  refactoring and formatting-only churn.
- Prefer the minimum solution that satisfies the requirement. Speculative
  abstraction and unused configuration will be sent back.
- Add or update tests for behaviour you change. Code in `jaime-package/` must
  stay testable without Juju.
- Update `CHANGELOG.md`, and `TASKS.md` if you complete or discover a task.
- All CI checks must pass: lint, the docs checks (codespell and mdformat), the
  three unit suites, packing, and the machine integration suite.
- State your assumptions in the PR description. If several readings of the
  requirement exist, say which you chose.

Asking a clarifying question is always preferred over guessing.

## Safety rules

Jaime runs next to production workloads and talks to third-party AI providers.
A pull request that breaks one of these will not be merged:

1. **No automatic remediation.** Jaime observes and reports. `act` mode is
   deliberately blocked, and AI output is advisory only.
2. **No arbitrary shell command execution**, and no AI-generated commands are
   ever run.
3. **Never log AI provider tokens or Juju secrets** — not in reports, audit
   logs, prompts, debug output or test fixtures.
4. **Never send unbounded logs to a provider.** Collected context must be
   bounded by time, lines or bytes, and stay within `max-context-lines`.
5. **Everything must be auditable.** Incident events are written as structured
   JSONL, context is persisted *before* it is sent to a provider, and reports
   reference the stored context they were generated from.
6. **Raw evidence is always retained.** AI output must never be the only source
   of truth.
7. **The charm must work with no AI provider configured**, producing a useful
   non-AI report.

Fail safely: write a structured JSONL error event and set a clear unit status.
Do not add speculative recovery logic.
