## Requirements

### Requirement: Operator-supplied k8s diagnostics plan

The k8s charm SHALL accept a `diagnostics` config option containing a JSON
object keyed by application name, and SHALL validate it on config-changed.

#### Scenario: valid plan
- **WHEN** `diagnostics` is set to a valid plan for a monitored application
- **THEN** the unit is not blocked
- **AND** the plan is applied on the next incident and the next `generate-report` regeneration

#### Scenario: invalid plan
- **WHEN** `diagnostics` is not valid JSON, fails the k8s schema, or exceeds a cap
- **THEN** the unit is `BlockedStatus` with the first validation error
- **AND** a structured JSONL error event is written

#### Scenario: no plan
- **WHEN** `diagnostics` is empty or `{}`
- **THEN** collection and the report are identical to the behaviour before this change

### Requirement: Plan-driven container selection

When a plan lists `containers`, the collector SHALL collect current and previous
logs only for those containers, and SHALL report listed containers that the pod
does not have.

#### Scenario: named container missing
- **WHEN** the plan names a container the pod does not have
- **THEN** the report marks it not found
- **AND** collection of the other containers continues

### Requirement: Env name and port checks

The collector SHALL compare plan `env_variables` against the container spec's
env names and plan `ports` against declared `containerPorts`, reporting each as
present or missing. It SHALL never include env values.

#### Scenario: env value never exposed
- **WHEN** an expected env variable is set from a literal or a `secretKeyRef`
- **THEN** the report shows it as set
- **AND** its value appears nowhere in the context, report or prompt

#### Scenario: port not declared
- **WHEN** a plan port is not declared by any selected container
- **THEN** the report marks it missing

### Requirement: Extra log patterns stay bounded

Lines matching plan `log_patterns` SHALL be kept in addition to error and
warning lines, within the existing per-item, per-line and section caps.

#### Scenario: pattern matches many lines
- **WHEN** a pattern matches more lines than `max-context-lines`
- **THEN** the retained lines are capped exactly as without a plan

### Requirement: Plan checks appear in the report

The report SHALL show plan container, env and port results, and SHALL describe a
port as declared rather than listening.

#### Scenario: declared port
- **WHEN** a plan port is declared by a selected container
- **THEN** the report marks it declared
- **AND** does not claim anything is listening
