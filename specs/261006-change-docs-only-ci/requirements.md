## Requirements

### Requirement: Docs-only changes run only the docs checks

A change that touches only documentation SHALL skip the code jobs, and a change
that touches code SHALL run the full pipeline.

#### Scenario: docs-only change
- **WHEN** a change touches only `**/*.md`, `docs/**`, `specs/**` or `.mdformat.toml`
- **THEN** `lint`, `unit`, `pack` and `integration` are skipped
- **AND** the docs checks run

#### Scenario: code change
- **WHEN** a change touches any other path
- **THEN** the full pipeline runs

#### Scenario: mixed change
- **WHEN** a change touches both documentation and code
- **THEN** the full pipeline runs
- **AND** the docs checks run as well

#### Scenario: workflow change
- **WHEN** a change touches `.github/**`
- **THEN** the full pipeline runs, so the workflow itself is exercised

### Requirement: Skipped jobs do not block merges

Jobs skipped for a docs-only change SHALL report as skipped rather than missing,
so required status checks are satisfied.

#### Scenario: required check on a docs-only change
- **WHEN** the code jobs are skipped on a docs-only change
- **THEN** the checks report as skipped
- **AND** the change is not blocked waiting for them
