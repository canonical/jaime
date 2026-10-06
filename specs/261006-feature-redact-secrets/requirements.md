## Requirements

### Requirement: Sensitive config values never reach the report

The report SHALL NOT render the value of a config option whose name is
sensitive, or whose Juju type is `secret`. The option name SHALL remain visible.

#### Scenario: sensitive-named option
- **WHEN** the watched application has an option named `password` with a value
- **THEN** the report shows the option with `[REDACTED]` in place of the value
- **AND** the value appears nowhere in the report or a prompt built from it

#### Scenario: secret-typed option
- **WHEN** an option is of Juju type `secret`
- **THEN** the report shows it as set or unset
- **AND** never renders a value

#### Scenario: shapeless secret value
- **WHEN** a sensitive-named option holds a random passphrase with no
  recognisable token shape
- **THEN** it is still redacted, because the option name decides

### Requirement: Recognisable secrets are scrubbed from report text

The report SHALL replace known secret shapes with `[REDACTED]` in logs,
health-command output, snap logs and the status message.

#### Scenario: token in a log line
- **WHEN** a collected log line contains a `Bearer` token or a Juju `secret:` URI
- **THEN** the report shows `[REDACTED]` in its place
- **AND** a prompt built from the report contains no such token

#### Scenario: ordinary values are preserved
- **WHEN** a log line contains a UUID, a commit SHA, a version or a path
- **THEN** the line is unchanged

### Requirement: Redaction is idempotent

Applying redaction to already-redacted output SHALL NOT change it.

#### Scenario: second pass
- **WHEN** a redacted report is passed through the redactor again
- **THEN** the output is byte-identical
