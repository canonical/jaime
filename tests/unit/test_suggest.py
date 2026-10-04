"""Unit tests for jaime.suggest (suggest/act engine)."""

import unittest.mock as mock

import pytest

from jaime.incident import Suggestion, UsageMetadata
from jaime.suggest import (
    build_suggest_prompt,
    execute_command,
    parse_commands,
    run_act,
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


class TestExecuteCommand:
    def test_successful_command(self):
        result = execute_command("echo hello")
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]

    def test_failed_command(self):
        result = execute_command("ls /nonexistent_path_xyz")
        assert result["returncode"] != 0

    def test_timeout_returns_error(self):
        result = execute_command("sleep 60", timeout=1)
        assert result["returncode"] == -1
        assert "timed out" in result["stderr"]


class TestRunSuggest:
    def test_returns_suggestion(self):
        provider = _make_provider("The issue is X.\n```bash\nsystemctl status\n```")
        result = run_suggest(provider, "report content")
        assert isinstance(result, Suggestion)
        assert "The issue is X." in result.description
        assert "systemctl status" in result.commands
        provider.generate.assert_called_once()

    def test_suggestion_carries_usage(self):
        usage = UsageMetadata(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                              cost_usd=0.001, model="deepseek/deepseek-chat")
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


class TestRunAct:
    def test_returns_suggestion_and_results(self):
        provider = _make_provider("```bash\necho hello\n```")
        suggestion, results = run_act(provider, "report")
        assert isinstance(suggestion, Suggestion)
        assert "echo hello" in suggestion.commands
        assert len(results) == 1
        assert results[0]["command"] == "echo hello"

    def test_executes_commands(self):
        provider = _make_provider("```bash\necho hello\n```")
        _, results = run_act(provider, "report")
        assert results[0]["returncode"] == 0
        assert "hello" in results[0]["stdout"]

    def test_dry_run_does_not_execute(self):
        provider = _make_provider("```bash\necho hello\n```")
        with mock.patch("jaime.suggest.execute_command") as mock_exec:
            _, results = run_act(provider, "report", dry_run=True)
        mock_exec.assert_not_called()
        assert "dry-run" in results[0]["stderr"]

    def test_returns_none_when_no_provider(self):
        suggestion, results = run_act(None, "report")
        assert suggestion is None
        assert results == []
