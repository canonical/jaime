"""Unit tests for the Incident model."""

import uuid

import pytest

from jaime.incident import Incident, Suggestion, UsageMetadata


class TestIncidentOpen:
    def test_open_returns_incident(self):
        inc = Incident.open()
        assert isinstance(inc, Incident)

    def test_id_is_valid_uuid(self):
        inc = Incident.open()
        uuid.UUID(inc.id)

    def test_each_open_has_unique_id(self):
        ids = {Incident.open().id for _ in range(10)}
        assert len(ids) == 10

    def test_opened_at_is_utc_iso(self):
        inc = Incident.open()
        assert "+00:00" in inc.opened_at or inc.opened_at.endswith("Z")

    def test_is_open_true_on_new_incident(self):
        assert Incident.open().is_open is True

    def test_closed_at_is_none_on_new_incident(self):
        assert Incident.open().closed_at is None

    def test_incident_is_immutable(self):
        inc = Incident.open()
        with pytest.raises((AttributeError, TypeError)):
            inc.id = "changed"


class TestIncidentClose:
    def test_close_returns_new_incident(self):
        inc = Incident.open()
        closed = inc.close()
        assert closed is not inc

    def test_close_preserves_id_and_opened_at(self):
        inc = Incident.open()
        closed = inc.close()
        assert closed.id == inc.id
        assert closed.opened_at == inc.opened_at

    def test_close_sets_closed_at(self):
        inc = Incident.open()
        closed = inc.close()
        assert closed.closed_at is not None
        assert "+00:00" in closed.closed_at or closed.closed_at.endswith("Z")

    def test_is_open_false_after_close(self):
        closed = Incident.open().close()
        assert closed.is_open is False

    def test_original_incident_unchanged_after_close(self):
        inc = Incident.open()
        inc.close()
        assert inc.is_open is True


class TestIncidentSerialisation:
    def test_to_dict_open_has_no_closed_at(self):
        inc = Incident.open()
        d = inc.to_dict()
        assert "id" in d
        assert "opened_at" in d
        assert "closed_at" not in d

    def test_to_dict_closed_includes_closed_at(self):
        closed = Incident.open().close()
        d = closed.to_dict()
        assert "closed_at" in d
        assert d["closed_at"] == closed.closed_at

    def test_from_dict_roundtrip_open(self):
        inc = Incident.open()
        restored = Incident.from_dict(inc.to_dict())
        assert restored.id == inc.id
        assert restored.opened_at == inc.opened_at
        assert restored.closed_at is None

    def test_from_dict_roundtrip_closed(self):
        closed = Incident.open().close()
        restored = Incident.from_dict(closed.to_dict())
        assert restored.id == closed.id
        assert restored.closed_at == closed.closed_at
        assert restored.is_open is False

    def test_from_dict_with_known_values(self):
        d = {"id": "550e8400-e29b-41d4-a716-446655440000", "opened_at": "2026-07-14T10:00:00+00:00"}
        inc = Incident.from_dict(d)
        assert inc.id == d["id"]
        assert inc.closed_at is None
        assert inc.status_message == ""


class TestIncidentStatusMessage:
    def test_open_accepts_message(self):
        inc = Incident.open(status_message="Please initialize OpenBao")
        assert inc.status_message == "Please initialize OpenBao"

    def test_open_defaults_to_empty(self):
        assert Incident.open().status_message == ""

    def test_message_survives_close(self):
        inc = Incident.open(status_message="disk full")
        closed = inc.close()
        assert closed.status_message == "disk full"

    def test_message_roundtrips_through_dict(self):
        inc = Incident.open(status_message="Please initialize OpenBao or integrate with an auto-unseal provider")
        restored = Incident.from_dict(inc.to_dict())
        assert restored.status_message == inc.status_message

    def test_empty_message_omitted_from_dict(self):
        d = Incident.open(status_message="").to_dict()
        assert "status_message" not in d

    def test_old_state_without_message_loads_as_empty(self):
        d = {"id": "x", "opened_at": "2026-07-14T10:00:00+00:00", "closed_at": None}
        assert Incident.from_dict(d).status_message == ""

    def test_message_preserved_after_suggestion_attach(self):
        inc = Incident.open(status_message="unseal needed")
        s = Suggestion.from_llm("diagnosis", ["bao operator unseal"])
        updated = inc.attach_suggestion(s)
        assert updated.status_message == "unseal needed"


class TestSuggestion:
    def test_from_llm_creates_suggestion(self):
        s = Suggestion.from_llm("The issue is X.", ["systemctl status", "journalctl -n 50"])
        assert s.description == "The issue is X."
        assert "systemctl status" in s.commands
        assert "journalctl -n 50" in s.commands
        assert "+00:00" in s.generated_at

    def test_commands_are_immutable_tuple(self):
        s = Suggestion.from_llm("desc", ["cmd1"])
        assert isinstance(s.commands, tuple)

    def test_to_dict_roundtrip(self):
        s = Suggestion.from_llm("desc", ["cmd1", "cmd2"])
        restored = Suggestion.from_dict(s.to_dict())
        assert restored.description == s.description
        assert restored.commands == s.commands
        assert restored.generated_at == s.generated_at

    def test_attach_suggestion_to_incident(self):
        inc = Incident.open()
        s = Suggestion.from_llm("diagnosis", ["df -h"])
        updated = inc.attach_suggestion(s)
        assert updated.suggestion is not None
        assert updated.suggestion.description == "diagnosis"
        assert inc.suggestion is None  # original unchanged

    def test_incident_with_suggestion_roundtrip(self):
        inc = Incident.open()
        s = Suggestion.from_llm("diagnosis", ["df -h"])
        updated = inc.attach_suggestion(s)
        restored = Incident.from_dict(updated.to_dict())
        assert restored.suggestion is not None
        assert restored.suggestion.description == "diagnosis"
        assert "df -h" in restored.suggestion.commands


class TestUsageMetadata:
    def test_defaults_are_zero(self):
        u = UsageMetadata()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0
        assert u.cost_usd is None
        assert u.model == ""

    def test_to_dict_omits_cost_when_none(self):
        u = UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="m")
        d = u.to_dict()
        assert "cost_usd" not in d
        assert d["prompt_tokens"] == 10
        assert d["total_tokens"] == 15

    def test_to_dict_includes_cost_when_set(self):
        u = UsageMetadata(cost_usd=0.001234)
        d = u.to_dict()
        assert d["cost_usd"] == 0.001234

    def test_from_dict_roundtrip(self):
        u = UsageMetadata(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                          cost_usd=0.005, model="deepseek/deepseek-chat")
        restored = UsageMetadata.from_dict(u.to_dict())
        assert restored.prompt_tokens == 100
        assert restored.completion_tokens == 50
        assert restored.cost_usd == 0.005
        assert restored.model == "deepseek/deepseek-chat"

    def test_from_dict_missing_cost_returns_none(self):
        u = UsageMetadata.from_dict({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        assert u.cost_usd is None

    def test_suggestion_carries_usage_in_roundtrip(self):
        usage = UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                              cost_usd=0.001, model="gemini-2.5-flash")
        s = Suggestion.from_llm("desc", ["cmd"], usage=usage)
        restored = Suggestion.from_dict(s.to_dict())
        assert restored.usage is not None
        assert restored.usage.prompt_tokens == 10
        assert restored.usage.cost_usd == 0.001

    def test_suggestion_without_usage_roundtrip(self):
        s = Suggestion.from_llm("desc", ["cmd"])
        assert s.usage is None
        restored = Suggestion.from_dict(s.to_dict())
        assert restored.usage is None
