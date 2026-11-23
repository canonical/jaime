## Requirements

### Requirement: hotsos is opt-in

hotsos SHALL NOT be invoked unless explicitly enabled.

#### Scenario: disabled by default
- **WHEN** `enable-hotsos` is false (the default)
- **THEN** jaime never invokes hotsos
- **AND** the report has no `## Hotsos analysis` section

### Requirement: the install is bounded and attempted once

#### Scenario: hotsos absent, enabled
- **WHEN** `enable-hotsos` is true and no `hotsos` is on `PATH`
- **THEN** jaime installs the pinned version into an isolated venv under
  `/var/lib/jaime/hotsos/`
- **AND** the whole install runs under a wall-clock timeout

#### Scenario: the install exceeds the timeout or fails
- **WHEN** the install times out, or fails (PyPI unreachable, `python3-venv`
  missing)
- **THEN** no `## Hotsos analysis` section is produced
- **AND** a structured JSONL event records the failure
- **AND** the install is not retried on later hooks for the same config
- **AND** the unit is not blocked

### Requirement: the install re-arms only on a config change

#### Scenario: re-enable or pin change
- **WHEN** `enable-hotsos` is toggled off then on, or the code pin changes on a
  charm upgrade
- **THEN** one fresh install attempt is allowed

### Requirement: an existing hotsos is preferred

#### Scenario: hotsos on PATH
- **WHEN** a `hotsos` binary is on `PATH`
- **THEN** jaime uses it as-is and installs nothing
- **AND** the version actually used is recorded

### Requirement: a hotsos failure never breaks collection

#### Scenario: hotsos absent, failing, or slow
- **WHEN** hotsos is not installed, or exits non-zero, or exceeds its timeout
- **THEN** collection continues with no hotsos section
- **AND** a structured JSONL event records the failure

### Requirement: only supported plugins run

#### Scenario: no plugin for the principal
- **WHEN** `hotsos-plugins` is empty and the principal's application has no
  matching hotsos plugin
- **THEN** only the generic plugins (`system`, `juju`) run
- **AND** the section says so

#### Scenario: operator overrides the plugin set
- **WHEN** `hotsos-plugins` is set
- **THEN** exactly that plugin list runs

### Requirement: the short digest reaches the context, the report and the prompt

#### Scenario: hotsos reports findings
- **WHEN** hotsos reports potential issues or detected bugs
- **THEN** only the short digest enters the context and appears under
  `## Hotsos analysis`
- **AND** it is redacted on write (4.9)
- **AND** it is Tier 1 in the prompt projection (6.1), reachable by
  `get-suggestion` and `ask`

### Requirement: the raw output stays operator-only

#### Scenario: full output requested
- **WHEN** the operator runs `run-hotsos output=full`
- **THEN** the raw JSON is written to a file and its path is returned
- **AND** the raw output never enters the context, the report or the prompt

### Requirement: a healthy unit can still surface a finding

#### Scenario: known bug on a healthy principal (variant)
- **WHEN** the principal is healthy but hotsos reports a known bug
- **THEN** jaime can record the finding as an independent signal without opening
  an AI incident
