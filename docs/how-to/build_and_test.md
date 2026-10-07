# Build and test Jaime

How to run the test suites, pack the charms, and build the documentation from a
source checkout.

## Dependencies

A Juju charm checkout needs `pytest`, `ops`, and `tox` for the automated checks.
The repository provides a `Makefile` that wraps the common operations.

## Unit tests

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

## Integration tests

`tests/integration/` deploys real charms against a real Juju controller and
drives a real fault through the incident → report → suggestion chain. These are
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

## Building the charms

Both charms build for Ubuntu 22.04, 24.04 and 26.04 on amd64. An amd64 host
produces the amd64 artifacts. The `uv` plugin resolves dependencies from each
charm's `pyproject.toml` and a committed `uv.lock`.

```bash
make pack-all      # both charms into dist/
make pack-machine  # machine charm only
make pack-k8s      # k8s charm only
make clean         # remove build output
make distclean     # also remove .venv/ and .tox/
```

## Building this documentation

```bash
make -C docs html  # build the site into docs/_build
make -C docs serve # run a live preview (sphinx-autobuild)
```

The site uses the Canonical Sphinx stack; the `docs/.venv` holds the build
dependencies.
