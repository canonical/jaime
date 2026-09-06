"""Unit tests for jaime.core (shared charm logic)."""

import unittest.mock as mock

from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness

from jaime.core import CoreMixin, Mode, Provider, summarise_usage
from jaime.incident import Incident
from jaime.principal import StatusTracker


class _DummyCharm(CoreMixin, CharmBase):
    """Minimal charm used to exercise the shared CoreMixin behaviour."""

    def __init__(self, *args):
        super().__init__(*args)
        self._status_tracker = StatusTracker()
        self.framework.observe(self.on.config_changed, self._on_config_changed)


# Declared inline rather than read from either charm's config.yaml. CoreMixin
# is substrate-independent, so its test must not depend on a particular
# charm's packaging; this also keeps the suite runnable from the repo root.
# Only the options CoreMixin itself reads are listed.
_SHARED_CONFIG_YAML = """
options:
  mode:
    type: string
    default: observe
  provider:
    type: string
    default: none
  model:
    type: string
    default: ""
  api-token:
    type: string
    default: ""
  watch-statuses:
    type: string
    default: "error,blocked"
  failure-timeout-minutes:
    type: int
    default: 5
  cooldown-minutes:
    type: int
    default: 30
  report-dir:
    type: string
    default: /var/log/jaime/reports
  audit-log-path:
    type: string
    default: /var/log/jaime/events.jsonl
"""


def _make_harness(config_overrides=None):
    h = Harness(_DummyCharm, config=_SHARED_CONFIG_YAML)
    h.begin()
    if config_overrides:
        h.update_config(config_overrides)
    return h


class TestConfigChanged:
    def test_invalid_mode_sets_blocked(self):
        h = _make_harness({"mode": "diagnose"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "invalid mode" in h.charm.unit.status.message

    def test_observe_mode_sets_ready(self):
        h = _make_harness({"mode": "observe"})
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"

    def test_invalid_provider_sets_blocked(self):
        h = _make_harness({"mode": "observe", "provider": "foobar"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "invalid provider" in h.charm.unit.status.message

    def test_act_mode_sets_blocked_not_implemented(self):
        h = _make_harness({"mode": "act"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "not yet implemented" in h.charm.unit.status.message

    def test_suggest_mode_no_provider_sets_blocked(self):
        h = _make_harness({"mode": "suggest", "provider": "none"})
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "provider is not configured" in h.charm.unit.status.message

    def test_valid_provider_and_token_sets_active(self):
        h = _make_harness({"mode": "suggest", "provider": "gemini", "api-token": "tok"})
        mock_provider = mock.MagicMock()
        mock_provider.check.return_value = None
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(mock_provider, None)):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)

    def test_bad_token_sets_blocked(self):
        h = _make_harness({"mode": "suggest", "provider": "gemini", "api-token": "bad"})
        mock_provider = mock.MagicMock()
        mock_provider.check.return_value = "HTTP 401: invalid"
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(mock_provider, None)):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, BlockedStatus)
        assert "AI provider error" in h.charm.unit.status.message

    def test_config_change_preserves_open_incident_status(self):
        """A config change must not overwrite an open incident's status."""
        h = _make_harness({"mode": "observe"})
        inc = Incident.open()
        h.charm._status_tracker._state["postgresql/0"] = {
            "status": "blocked",
            "since": "2026-01-01T00:00:00+00:00",
            "increment": 3,
            "incident": inc.to_dict(),
            "last_reported": "2026-01-01T00:01:00+00:00",
        }
        with mock.patch.object(h.charm, "_get_ai_provider", return_value=(None, "no provider")):
            h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert "incident open" in h.charm.unit.status.message
        assert inc.id[:8] in h.charm.unit.status.message

    def test_config_change_no_incident_sets_ready(self):
        h = _make_harness({"mode": "observe"})
        h.charm._on_config_changed(mock.MagicMock())
        assert isinstance(h.charm.unit.status, ActiveStatus)
        assert h.charm.unit.status.message == "Ready"


class TestEnums:
    def test_mode_values(self):
        assert Mode.OBSERVE.value == "observe"
        assert Mode.SUGGEST.value == "suggest"
        assert Mode.ACT.value == "act"

    def test_provider_values(self):
        assert Provider.NONE.value == "none"
        assert Provider.GEMINI.value == "gemini"
        assert Provider.OPENROUTER.value == "openrouter"


class TestSummariseUsage:
    def test_empty(self):
        assert summarise_usage([]) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": None,
            "by_model": {},
        }

    def test_rolls_up_single_entry(self):
        result = summarise_usage([{
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.001, "model": "deepseek/deepseek-chat",
        }])
        assert result["total_tokens"] == 150
        assert result["cost_usd"] == 0.001
        assert result["by_model"]["deepseek/deepseek-chat"]["calls"] == 1

    def test_breaks_down_by_model(self):
        result = summarise_usage([
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "cost_usd": 0.001, "model": "m1"},
            {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
             "cost_usd": 0.002, "model": "m2"},
        ])
        assert result["total_tokens"] == 45
        assert result["by_model"]["m1"]["total_tokens"] == 15
        assert result["by_model"]["m2"]["total_tokens"] == 30
        assert abs(result["cost_usd"] - 0.003) < 1e-9

    def test_missing_cost_yields_none(self):
        result = summarise_usage([{
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
            "model": "gemini-2.5-flash",
        }])
        assert result["cost_usd"] is None
        assert result["by_model"]["gemini-2.5-flash"]["cost_usd"] is None


class TestResolveSecret:
    def _make(self, model):
        charm = object.__new__(CoreMixin)
        charm.model = model
        return charm

    def test_plain_value_returned(self):
        model = mock.MagicMock()
        model.config.get.return_value = "plain-token"
        charm = self._make(model)
        assert charm._resolve_api_token() == "plain-token"

    def test_secret_resolution(self):
        model = mock.MagicMock()
        model.config.get.return_value = "secret:abc123"
        secret = mock.MagicMock()
        secret.get_content.return_value = {"token": "the-token"}
        model.get_secret.return_value = secret
        charm = self._make(model)
        assert charm._resolve_api_token() == "the-token"

    def test_secret_error_returns_empty(self):
        model = mock.MagicMock()
        model.config.get.return_value = "secret:abc123"
        model.get_secret.side_effect = Exception("denied")
        charm = self._make(model)
        assert charm._resolve_api_token() == ""


class TestGetAIProvider:
    def _make(self, config: dict):
        model = mock.MagicMock()
        model.config.get.side_effect = lambda key, default=None: config.get(key, default)
        charm = object.__new__(CoreMixin)
        charm.model = model
        return charm

    def test_provider_none(self):
        charm = self._make({"provider": "none"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "provider is not configured" in err

    def test_missing_token(self):
        charm = self._make({"provider": "gemini", "api-token": ""})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "api-token is not set" in err

    def test_unsupported_provider(self):
        charm = self._make({"provider": "foo"})
        provider, err = charm._get_ai_provider()
        assert provider is None
        assert "unsupported provider" in err

    def test_gemini_provider(self):
        charm = self._make({"provider": "gemini", "api-token": "tok"})
        provider, err = charm._get_ai_provider()
        from jaime.providers.gemini import GeminiProvider
        assert isinstance(provider, GeminiProvider)
        assert err is None

    def test_default_model(self):
        charm = self._make({})
        assert charm._default_model("gemini") == "gemini-2.5-flash"
        assert charm._default_model("openrouter") == "~deepseek/deepseek-v4-flash-latest"
