## Requirements

### Requirement: hotsos is opt-in

hotsos SHALL NOT be invoked unless explicitly enabled.

#### Scenario: disabled by default
- **WHEN** `enable-hotsos` is false (the default)
- **THEN** jaime never invokes hotsos
- **AND** the report has no `## Hotsos analysis` section

### Requirement: a hotsos failure never breaks collection

#### Scenario: hotsos absent, failing, or slow
- **WHEN** hotsos is not installed, or exits non-zero, or exceeds its timeout
- **THEN** collection continues with no hotsos section
- **AND** a structured JSONL event records the failure

### Requirement: only supported plugins run

#### Scenario: no plugin for the principal
- **WHEN** the principal's application has no matching hotsos plugin
- **THEN** only the generic plugins (`system`, `juju`) run
- **AND** the section says so

### Requirement: the short digest reaches the report and the prompt

#### Scenario: hotsos reports findings
- **WHEN** hotsos reports potential issues or detected bugs
- **THEN** the short digest appears under `## Hotsos analysis`
- **AND** it is redacted on write (4.9)
- **AND** it is Tier 1 in the prompt projection (6.1), reachable by
  `get-suggestion` and `ask`

### Requirement: the raw output stays operator-only

#### Scenario: full output requested
- **WHEN** the operator runs `run-hotsos output=full`
- **THEN** the raw JSON is written to a file and its path is returned
- **AND** the raw output never enters the report or the prompt

### Requirement: a healthy unit can still surface a finding

#### Scenario: known bug on a healthy principal (variant)
- **WHEN** the principal is healthy but hotsos reports a known bug
- **THEN** jaime can record the finding as an independent signal without opening
  an AI incident
