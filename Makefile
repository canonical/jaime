# Makefile for Juju Charm Development
#
# Both charms build into dist/. Each pack target cleans only its own
# scratch state, so packing one charm never destroys the other's artifact.

PROJECT_NAME  = jaime
DIST_DIR      = dist
MACHINE_DIR   = charms/machine
K8S_DIR       = charms/k8s
VENDOR_NAME   = _jaime-package

# Principal application the machine subordinate relates to.
PRINCIPAL_CHARM ?= $(JAIME_PRINCIPAL_CHARM)

.PHONY: help lint test test-shared test-machine test-k8s \
        pack pack-all pack-machine pack-k8s \
        integration integration-machine integration-k8s \
        examples clean distclean check-principal deploy deploy-k8s remove remove-k8s

help:
	@echo "Available commands:"
	@echo "  make test          - Run all three unit suites"
	@echo "  make test-shared   - Run the shared jaime-package suite"
	@echo "  make test-machine  - Run the machine charm suite"
	@echo "  make test-k8s      - Run the k8s charm suite"
	@echo "  make lint          - Run ruff over the repository"
	@echo "  make examples      - Regenerate examples/ from the report generator"
	@echo "  make integration   - Integration tests (needs a Juju controller)"
	@echo "  make pack-machine  - Pack the machine subordinate charm into $(DIST_DIR)/"
	@echo "  make pack-k8s      - Pack the Kubernetes standalone charm into $(DIST_DIR)/"
	@echo "  make pack-all      - Pack both charms in one run"
	@echo "  make clean         - Remove build scratch and packed artifacts"
	@echo "  make distclean     - clean, plus remove .venv/ and .tox/"
	@echo "  make deploy        - Pack and deploy the machine charm (needs PRINCIPAL_CHARM)"
	@echo "  make remove        - Remove the machine charm from the current model"

# ---------------------------------------------------------------------------
# Test and lint
# ---------------------------------------------------------------------------

test:
	./scripts/test.sh

test-shared:
	pytest tests/unit/

test-machine:
	cd $(MACHINE_DIR) && pytest tests/

test-k8s:
	cd $(K8S_DIR) && pytest tests/

lint:
	ruff check .

examples:
	@if [ -x .venv/bin/python ]; then .venv/bin/python scripts/generate_examples.py; \
	else python3 scripts/generate_examples.py; fi

# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------
#
# These drive a real Juju controller and are excluded from `make test`.
# They need a bootstrapped controller and packed charms in dist/.

integration: pack-all
	pytest tests/integration/ -v

integration-machine:
	pytest tests/integration/test_machine.py tests/integration/test_flapping.py -v

integration-k8s:
	pytest tests/integration/test_k8s.py -v

# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------
#
# charmcraft's managed container only sees files inside the charm directory,
# so the shared library must be physically present (not a symlink) at pack
# time. Tests use the .vendored/ symlink instead; both mechanisms are needed.

$(DIST_DIR):
	mkdir -p $(DIST_DIR)

pack-machine: | $(DIST_DIR)
	@echo "Packing machine charm..."
	rm -rf $(MACHINE_DIR)/$(VENDOR_NAME)
	cp -r jaime-package $(MACHINE_DIR)/$(VENDOR_NAME)
	cd $(MACHINE_DIR) && charmcraft pack
	mv $(MACHINE_DIR)/*.charm $(DIST_DIR)/
	rm -rf $(MACHINE_DIR)/$(VENDOR_NAME)
	@echo "Done: $$(ls -1 $(DIST_DIR)/jaime_*.charm)"

pack-k8s: | $(DIST_DIR)
	@echo "Packing k8s charm..."
	rm -rf $(K8S_DIR)/$(VENDOR_NAME)
	cp -r jaime-package $(K8S_DIR)/$(VENDOR_NAME)
	cd $(K8S_DIR) && charmcraft pack
	mv $(K8S_DIR)/*.charm $(DIST_DIR)/
	rm -rf $(K8S_DIR)/$(VENDOR_NAME)
	@echo "Done: $$(ls -1 $(DIST_DIR)/jaime-k8s_*.charm)"

pack-all: pack-machine pack-k8s
	@echo "Packed artifacts:"
	@ls -1 $(DIST_DIR)/*.charm

# Backwards-compatible alias.
pack: pack-all

# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
#
# clean removes build output only. Virtualenvs are expensive to rebuild and
# are not build output, so they are removed by distclean instead.

clean:
	rm -rf $(DIST_DIR)/ build/ *.charm
	rm -rf $(MACHINE_DIR)/$(VENDOR_NAME) $(K8S_DIR)/$(VENDOR_NAME)
	rm -f $(MACHINE_DIR)/*.charm $(K8S_DIR)/*.charm
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf .venv/ .tox/ .coverage

# ---------------------------------------------------------------------------
# Deployment helpers
# ---------------------------------------------------------------------------

# Fail before doing any expensive packing work.
check-principal:
	@test -n "$(PRINCIPAL_CHARM)" || { \
		echo "ERROR: set PRINCIPAL_CHARM=<application>, the principal to relate to."; \
		echo "       e.g. make deploy PRINCIPAL_CHARM=postgresql"; \
		exit 1; }

deploy: check-principal pack-machine
	@echo "Deploying machine charm alongside $(PRINCIPAL_CHARM)..."
	juju deploy ./$(DIST_DIR)/jaime_ubuntu-24.04-amd64.charm --force \
		--config provider="$(JAIME_PROVIDER)" \
		--config model="$(JAIME_MODEL)" \
		--config api-token="$(JAIME_API_TOKEN)" \
		--config watch-statuses="error,blocked" \
		--config failure-timeout-minutes=1
	juju relate jaime $(PRINCIPAL_CHARM)

deploy-k8s: pack-k8s
	@echo "Deploying k8s charm..."
	@echo "NOTE: the application must be named jaime-k8s; the RoleBinding in"
	@echo "      $(K8S_DIR)/jaime-k8s-rbac.yaml is bound to that ServiceAccount."
	juju deploy ./$(DIST_DIR)/jaime-k8s_ubuntu-24.04-amd64.charm jaime-k8s --trust \
		--config provider="$(JAIME_PROVIDER)" \
		--config model="$(JAIME_MODEL)" \
		--config api-token="$(JAIME_API_TOKEN)" \
		--config watch-applications="$(JAIME_WATCH_APPLICATIONS)" \
		--config watch-statuses="error,blocked" \
		--config failure-timeout-minutes=1

remove: check-principal
	@echo "Removing the machine charm from the current Juju model..."
	-juju remove-relation jaime $(PRINCIPAL_CHARM)
	juju remove-application jaime --no-prompt

remove-k8s:
	@echo "Removing the k8s charm from the current Juju model..."
	juju remove-application jaime-k8s --no-prompt
