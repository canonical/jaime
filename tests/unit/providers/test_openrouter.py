import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from jaime.incident import UsageMetadata
from jaime.providers.openrouter import OpenRouterProvider


def _mock_urlopen_response(data_bytes):
    """Create a mock urlopen return that works as a context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = data_bytes
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = None
    return mock_cm


def _http_error(code=400, body=b'{"error": "invalid"}'):
    # HTTPError delegates read() to its underlying fp, so the body must be
    # set on the fp rather than on the error object itself.
    fp = MagicMock()
    fp.read.return_value = body
    return urllib.error.HTTPError(
        url="http://example.com",
        code=code,
        msg="Bad Request",
        hdrs={},
        fp=fp,
    )


class TestOpenRouterInit:
    def test_init_default_model(self):
        provider = OpenRouterProvider("test-token")
        assert provider._api_token == "test-token"
        assert provider._model == "~deepseek/deepseek-v4-flash-latest"

    def test_init_custom_model(self):
        provider = OpenRouterProvider("test-token", "anthropic/claude-3.5-sonnet")
        assert provider._model == "anthropic/claude-3.5-sonnet"


class TestOpenRouterCheck:
    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_check_success_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b'{"data": {}}')
        assert OpenRouterProvider("test-token").check() is None

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_check_sends_bearer_token(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b"{}")
        OpenRouterProvider("secret-token").check()

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret-token"
        assert req.get_method() == "GET"

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_check_http_error_returns_message(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(401, b"unauthorized")
        result = OpenRouterProvider("bad-token").check()

        assert result is not None
        assert "HTTP 401" in result
        assert "unauthorized" in result

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_check_connection_error_returns_message(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection failed")
        result = OpenRouterProvider("test-token").check()

        assert result is not None
        assert "connection error" in result

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_check_never_leaks_token_in_message(self, mock_urlopen):
        """A failed check must not echo the API token back to the caller."""
        mock_urlopen.side_effect = _http_error(403, b"forbidden")
        result = OpenRouterProvider("super-secret-token").check()

        assert "super-secret-token" not in result


class TestOpenRouterGenerate:
    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_success(self, mock_urlopen):
        payload = json.dumps({
            "choices": [{"message": {"content": "Hello from OpenRouter"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
                "cost": 0.00042,
            },
        }).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        provider = OpenRouterProvider("test-token")
        text, usage = provider.generate("say hello")

        assert text == "Hello from OpenRouter"
        assert isinstance(usage, UsageMetadata)
        assert usage.prompt_tokens == 12
        assert usage.completion_tokens == 7
        assert usage.total_tokens == 19
        assert usage.cost_usd == 0.00042
        assert usage.model == "~deepseek/deepseek-v4-flash-latest"

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_posts_model_and_prompt(self, mock_urlopen):
        payload = json.dumps({
            "choices": [{"message": {"content": "ok"}}],
        }).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        provider = OpenRouterProvider("test-token", "custom/model")
        provider.generate("diagnose this")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert req.get_method() == "POST"
        assert body["model"] == "custom/model"
        assert body["messages"] == [{"role": "user", "content": "diagnose this"}]

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_missing_usage_returns_zeros(self, mock_urlopen):
        """When usage is absent, token counts default to zero and cost is None."""
        payload = json.dumps({
            "choices": [{"message": {"content": "Hello"}}],
        }).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, usage = OpenRouterProvider("test-token").generate("say hello")

        assert text == "Hello"
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.cost_usd is None

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_null_content_returns_empty_string(self, mock_urlopen):
        """Tool-call responses carry content: null; that must not become 'None'."""
        payload = json.dumps({
            "choices": [{"message": {"content": None}}],
        }).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, _ = OpenRouterProvider("test-token").generate("test")
        assert text == ""

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_missing_message_returns_empty_string(self, mock_urlopen):
        payload = json.dumps({"choices": [{}]}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, _ = OpenRouterProvider("test-token").generate("test")
        assert text == ""

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_no_choices_raises(self, mock_urlopen):
        payload = json.dumps({"choices": []}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        provider = OpenRouterProvider("test-token")
        with pytest.raises(RuntimeError, match="no choices"):
            provider.generate("test")

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_http_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(400)

        provider = OpenRouterProvider("test-token")
        with pytest.raises(urllib.error.HTTPError):
            provider.generate("test")

    @patch("jaime.providers.openrouter.urllib.request.urlopen")
    def test_generate_url_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection failed")

        provider = OpenRouterProvider("test-token")
        with pytest.raises(urllib.error.URLError):
            provider.generate("test")
