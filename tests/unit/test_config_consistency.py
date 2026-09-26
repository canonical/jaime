"""Assert the two charms' shared config options stay in step.

The machine charm (`charms/machine`) and the k8s charm (`charms/k8s`) live in
one repository and share most configuration, but nothing stopped their option
definitions from drifting apart. They already had — see TASKS 4.4.

The intersection of the two option sets is computed rather than hardcoded, so a
key that moves into the shared set (for example `diagnostics` when 4.6 lands) is
covered automatically without editing this file.
"""

import pathlib

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CONFIGS = {
    "machine": _REPO_ROOT / "charms" / "machine" / "config.yaml",
    "k8s": _REPO_ROOT / "charms" / "k8s" / "config.yaml",
}

# Shared options whose descriptions legitimately differ because their reach
# differs. The machine charm watches units on its own host; the k8s charm
# watches applications across the model namespace. Their wording is
# substrate-specific on purpose, so description equality is not asserted.
# Adding a key here needs a stated reason.
_SUBSTRATE_SPECIFIC = {"juju-api-user", "watch-applications"}


def _options(substrate: str) -> dict:
    return yaml.safe_load(_CONFIGS[substrate].read_text())["options"]


def _shared_keys() -> set[str]:
    return set(_options("machine")) & set(_options("k8s"))


def _normalise(text) -> str:
    """Collapse folded-scalar whitespace so comparison is formatting-agnostic."""
    return " ".join((text or "").split())


class TestConfigFiles:
    def test_both_config_files_exist_and_define_options(self):
        for substrate, path in _CONFIGS.items():
            assert path.exists(), f"{substrate} config.yaml missing at {path}"
            assert _options(substrate), f"{substrate} config.yaml defines no options"


class TestSharedOptions:
    def test_shared_option_set_is_not_empty(self):
        # A path mistake must not make every other assertion vacuously pass.
        assert _shared_keys(), "no shared options found; config paths are wrong?"

    def test_shared_options_have_matching_type_and_default(self):
        machine, k8s = _options("machine"), _options("k8s")
        for key in sorted(_shared_keys()):
            m_opt, k_opt = machine[key], k8s[key]
            assert m_opt.get("type") == k_opt.get("type"), (
                f"{key}: type differs ({m_opt.get('type')!r} vs {k_opt.get('type')!r})"
            )
            m_default, k_default = m_opt.get("default"), k_opt.get("default")
            assert type(m_default) is type(k_default) and m_default == k_default, (
                f"{key}: default differs ({m_default!r} vs {k_default!r})"
            )

    def test_shared_descriptions_are_aligned_except_substrate_specific(self):
        machine, k8s = _options("machine"), _options("k8s")
        for key in sorted(_shared_keys()):
            m_desc = _normalise(machine[key].get("description"))
            k_desc = _normalise(k8s[key].get("description"))
            assert m_desc, f"{key}: empty description in the machine charm"
            assert k_desc, f"{key}: empty description in the k8s charm"
            if key in _SUBSTRATE_SPECIFIC:
                continue
            assert m_desc == k_desc, (
                f"{key}: descriptions differ but the key is not allowlisted as "
                f"substrate-specific.\n  machine: {m_desc}\n  k8s:     {k_desc}"
            )

    def test_substrate_specific_allowlist_has_no_stale_entries(self):
        stale = _SUBSTRATE_SPECIFIC - _shared_keys()
        assert not stale, f"allowlisted as substrate-specific but not shared: {sorted(stale)}"
