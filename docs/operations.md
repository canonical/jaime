# Operations

How Jaime behaves once deployed, plus contributor build and test notes. For
installation see [Installing on a machine](install-machine.md) or
[Installing on Kubernetes](install-k8s.md).

## Incident flow

```text
Jaime
-> identifies the units to monitor
-> checks unit status on every update-status
-> detects a watched status (error/blocked by default)
-> tracks how long the unit stays unhealthy
-> after failure-timeout-minutes, opens an incident
-> collects bounded context (logs, plan checks, host or pod evidence)
-> writes a Markdown report and a JSONL audit event
-> in suggest mode, sends the stored report to the provider and attaches the suggestion
-> respects cooldown-minutes before the next report
-> closes the incident on recovery
```

The unhealthy timer is anchored to when Jaime first saw the unit go unhealthy,
not to Juju's `since` timestamp. A workload retrying in a loop re-sets its status
on every hook, so relying on Juju's value would reset the timer indefinitely and
never open an incident.

## Modes

- **`observe`** (default): collect context, write reports and audit events. On
  the machine charm the AI provider is still used once, to generate the
  diagnostics plan when the principal relation is joined; with no provider an
  empty plan is written and Jaime falls back to broad commands.
- **`suggest`**: as observe, plus a diagnosis. Jaime sends the already-written
  report to the provider and attaches the root-cause description and one
  suggested command to the incident, retrievable with `get-suggestion`. The
  suggestion is not merged into the report, and nothing is executed.
- **`act`**: not implemented. Setting it blocks the charm; no command is ever
  executed today.

## Monitoring scope

The machine charm monitors only units on its own host; the Kubernetes charm
monitors any application in the model namespace. If a machine hosts several
principal units and Jaime is related to more than one of them, Juju places a
Jaime unit alongside each. With `watch-applications` set, those units would
monitor the same host and report the same fault twice. Jaime flags this in its
unit status; relate it to one principal per machine to avoid it.

## Diagnostics plan

The plan drives what is collected.

- On the machine charm it is generated once on `principal-relation-joined`, from
  the `diagnostics` option or by the AI provider, and stored at
  `/var/lib/jaime/diagnostics.json`. `config-changed` does not regenerate it.
- On Kubernetes it is read from the `diagnostics` option at collection time.

An empty plan falls back to broad commands on the machine charm and to the fixed
pod collection on Kubernetes. See the [configuration reference](config.md) for
the option and both plan formats.

## Secret redaction

Reports redact sensitive config values and recognisable secrets before they are
written, so the persisted report and the prompt built from it contain none. The
audit log and the persisted `status-state.json` are not redacted; do not put
secrets in a workload status message. See
[the configuration reference](config.md#secret-redaction).

## Troubleshooting

- `show-status` lists the units actually observed. A configured application that
  is missing has no unit in reach.
- A blocked Kubernetes charm usually means missing or rejected `juju-api`
  credentials, missing Kubernetes RBAC, or a `watch-applications` name that is
  not on the model.
- An empty machine diagnostics plan means the provider was configured after
  relating; remove and re-add the relation.
- `generate-report` and `get-suggestion` fail when no incident is open.

## Development

### Testing

There are three unit suites: the shared `jaime-package` library, and one per
charm. Run all of them with:

```bash
make test          # or: ./scripts/test.sh
```

Individually:

```bash
make test-shared   # jaime-package  (tests/unit/)
make test-machine  # machine charm  (charms/machine/tests/)
make test-k8s      # k8s charm      (charms/k8s/tests/)
```

Or via tox, which also provides the lint environment:

```bash
tox                # lint + all three unit suites
tox -e lint        # ruff only
```

Tests are split by what they exercise rather than by which charm hosts them.
`tests/unit/` covers the shared library and sees only `jaime-package` on its
`pythonpath`, so it cannot accidentally depend on a charm-local module.
`charms/*/tests/` holds only substrate-specific tests.

### Integration tests

`tests/integration/` deploys real charms against a real Juju controller and
drives a real fault through the incident -> report -> suggestion chain. These are
excluded from every default test path and never run as part of `make test`.

```bash
make integration           # both substrates; packs the charms first
make integration-machine   # needs an LXD controller
make integration-k8s       # needs a MicroK8s controller
```

Set `JAIME_TEST_API_TOKEN` (and optionally `JAIME_TEST_PROVIDER`, default
`gemini`) to exercise the AI paths. With no token the provider-dependent tests
skip and the non-AI fallback path is tested instead. Pass `--keep-models` to
leave the temporary Juju model up for post-mortem inspection.

### Building

```bash
make pack-all      # both charms into dist/
make pack-machine  # machine charm only
make pack-k8s      # k8s charm only
make clean         # remove build output
make distclean     # also remove .venv/ and .tox/
```