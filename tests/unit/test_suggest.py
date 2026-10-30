"""Unit tests for jaime.suggest (suggest engine)."""

import unittest.mock as mock

import pytest

from jaime.incident import Suggestion, UsageMetadata
from jaime.suggest import (
    build_suggest_prompt,
    parse_commands,
    run_suggest,
)


def _make_provider(response_text: str, usage: UsageMetadata | None = None):
    """Return a mock provider whose generate() returns (text, usage)."""
    provider = mock.MagicMock()
    provider.generate.return_value = (
        response_text,
        usage or UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="test-model"),
    )
    return provider


class TestBuildSuggestPrompt:
    def test_contains_report(self):
        prompt = build_suggest_prompt("some report content")
        assert "some report content" in prompt

    def test_contains_instructions(self):
        prompt = build_suggest_prompt("report")
        assert "root cause" in prompt.lower()
        assert "bash" in prompt

    def test_warns_against_inventing_actions(self):
        prompt = build_suggest_prompt("report")
        assert "Available actions" in prompt
        assert "never invent an action" in prompt

    def test_urges_use_of_charm_links(self):
        prompt = build_suggest_prompt("report")
        assert "Charm links" in prompt
        assert "Never invent a URL or a procedure" in prompt


class TestParseCommands:
    def test_only_bash_fences_are_commands(self):
        # A lenient parser would surface guesses such as `systemctl restart`
        # from a model that ignored the format; no command beats a wrong one.
        response = "```sh\nsystemctl restart postgresql\n```\n```sql\nSELECT 1;\n```\n```yaml\na: b\n```"
        assert parse_commands(response) == []

    def test_extracts_single_command(self):
        response = "Here is a command:\n```bash\nsystemctl status postgresql\n```"
        assert parse_commands(response) == ["systemctl status postgresql"]

    def test_extracts_multiple_commands(self):
        response = "```bash\nsystemctl status postgresql\njournalctl -u postgresql -n 50\n```"
        result = parse_commands(response)
        assert "systemctl status postgresql" in result
        assert "journalctl -u postgresql -n 50" in result

    def test_ignores_comment_lines(self):
        response = "```bash\n# check status\nsystemctl status postgresql\n```"
        result = parse_commands(response)
        assert result == ["systemctl status postgresql"]

    def test_ignores_non_bash_blocks(self):
        response = "```python\nprint('hello')\n```"
        assert parse_commands(response) == []

    def test_empty_response_returns_empty(self):
        assert parse_commands("") == []

    def test_multiple_blocks(self):
        response = "```bash\ndf -h\n```\nsome text\n```bash\nfree -h\n```"
        result = parse_commands(response)
        assert "df -h" in result
        assert "free -h" in result


class TestRunSuggest:
    def test_returns_suggestion(self):
        provider = _make_provider("The issue is X.\n```bash\nsystemctl status\n```")
        result = run_suggest(provider, "report content")
        assert isinstance(result, Suggestion)
        assert "The issue is X." in result.description
        assert "systemctl status" in result.commands
        provider.generate.assert_called_once()

    def test_suggestion_carries_usage(self):
        usage = UsageMetadata(
            prompt_tokens=100, completion_tokens=50, total_tokens=150, cost_usd=0.001, model="deepseek/deepseek-chat"
        )
        provider = _make_provider("diagnosis\n```bash\ndf -h\n```", usage=usage)
        result = run_suggest(provider, "report content")
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 50
        assert result.usage.cost_usd == 0.001
        assert result.usage.model == "deepseek/deepseek-chat"

    def test_returns_none_when_no_provider(self):
        assert run_suggest(None, "report content") is None

    def test_raises_on_provider_failure(self):
        provider = mock.MagicMock()
        provider.generate.side_effect = RuntimeError("API error")
        with pytest.raises(RuntimeError):
            run_suggest(provider, "report content")


class TestHistorySection:
    def test_empty_history_returns_empty(self):
        from jaime.suggest import build_history_section

        assert build_history_section(None) == ""
        assert build_history_section([]) == ""

    def test_renders_questions_answers_and_commands(self):
        from jaime.suggest import build_history_section

        section = build_history_section(
            [
                {
                    "id": 2,
                    "question": "what is it?",
                    "description": "Postgres is down.",
                    "commands": ["juju config a=b"],
                },
            ]
        )
        assert "Turn 2 - operator: what is it?" in section
        assert "Postgres is down." in section
        assert "juju config a=b" in section

    def test_turn_without_question_is_the_initial_suggestion(self):
        from jaime.suggest import build_history_section

        section = build_history_section([{"id": 1, "question": "", "description": "first"}])
        assert "initial suggestion" in section

    def test_bounded_to_recent_turns_and_truncated(self):
        from jaime.suggest import build_history_section

        history = [{"id": i, "question": f"q{i}", "description": "x" * 1000} for i in range(1, 16)]
        section = build_history_section(history)
        assert "q5" not in section and "q6" in section and "q15" in section
        assert "x" * 500 not in section
        assert "..." in section


class TestAskPrompt:
    def test_orders_report_history_then_question(self):
        from jaime.suggest import build_ask_prompt

        prompt = build_ask_prompt(
            "REPORT",
            "why now?",
            history=[{"id": 1, "question": "earlier?", "description": "earlier answer"}],
        )
        assert prompt.index("REPORT") < prompt.index("earlier?") < prompt.index("why now?")

    def test_is_not_the_remediation_prompt(self):
        from jaime.suggest import build_ask_prompt, build_suggest_prompt

        prompt = build_ask_prompt("REPORT", "what is the lag?")
        assert "Operator-supplied context" not in prompt
        assert "single most likely remediation" not in prompt
        assert "single most likely remediation" in build_suggest_prompt("REPORT")

    def test_suggest_prompt_has_no_history(self):
        from jaime.suggest import build_suggest_prompt

        assert "Previous conversation" not in build_suggest_prompt("REPORT", "ctx")

    def test_run_ask_returns_suggestion_hashed_on_question(self):
        import hashlib

        from jaime.suggest import run_ask

        provider = mock.MagicMock()
        provider.generate.return_value = ("answer\n```bash\njuju config a=b\n```", None)
        s = run_ask(provider, "REPORT", "why?", history=[])
        assert s.commands == ("juju config a=b",)
        assert s.context_hash == hashlib.sha256(b"why?").hexdigest()
        assert "why?" in provider.generate.call_args[0][0]

    def test_run_ask_none_provider(self):
        from jaime.suggest import run_ask

        assert run_ask(None, "R", "q") is None


class TestPromptFormatReminder:
    def test_suggest_prompt_repeats_the_format_after_the_report(self):
        prompt = build_suggest_prompt("REPORT-BODY")
        tail = prompt[prompt.index("REPORT-BODY") :]
        assert "tagged with 'bash'" in tail
        assert 'under "Available actions"' in tail

    def test_suggest_prompt_still_ends_with_the_reminder_with_context(self):
        prompt = build_suggest_prompt("REPORT-BODY", "extra context")
        assert prompt.rstrip().endswith('appears under "Available actions" in the report.')

    def test_ask_prompt_ends_with_a_reminder_after_the_question(self):
        from jaime.suggest import build_ask_prompt

        prompt = build_ask_prompt("REPORT-BODY", "why is it blocked?")
        tail = prompt[prompt.index("why is it blocked?") :]
        assert "tagged with 'bash'" in tail
