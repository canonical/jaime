import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from jaime.incident import UsageMetadata
from jaime.providers.remote import RemoteProvider

BASE_URL = "http://localhost:11434/v1"


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


def _provider(token="", model="llama3.2", base=BASE_URL):
    return RemoteProvider(token, model, base)


class TestRemoteInit:
    def test_normalizes_trailing_slash(self):
        provider = RemoteProvider("", "m", "http://host:11434/v1/")
        assert provider._base_url == "http://host:11434/v1"

    def test_init_store_fields(self):
        provider = _provider("tok", "custom/model", "http://x/v1")
        assert provider._api_token == "tok"
        assert provider._model == "custom/model"
        assert provider._base_url == "http://x/v1"


class TestRemoteCheck:
    @patch("urllib.request.urlopen")
    def test_check_probes_models_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b'{"data": []}')
        assert _provider().check() is None

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == f"{BASE_URL}/models"
        assert req.get_method() == "GET"

    @patch("urllib.request.urlopen")
    def test_check_sends_auth_only_when_token_set(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b"{}")

        _provider("secret").check()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret"

        mock_urlopen.reset_mock()
        _provider().check()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None

    @patch("urllib.request.urlopen")
    def test_check_http_error_returns_message(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(404, b"not found")
        result = _provider().check()

        assert result is not None
        assert "HTTP 404" in result

    @patch("urllib.request.urlopen")
    def test_check_connection_error_returns_message(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        result = _provider().check()

        assert result is not None
        assert "connection error" in result

    @patch("urllib.request.urlopen")
    def test_check_never_leaks_token_in_message(self, mock_urlopen):
        """A failed check must not echo the API token back to the caller."""
        mock_urlopen.side_effect = _http_error(403, b"forbidden")
        result = _provider("super-secret-token").check()

        assert "super-secret-token" not in result


class TestRemoteGenerate:
    @patch("urllib.request.urlopen")
    def test_generate_success(self, mock_urlopen):
        payload = json.dumps(
            {
                "choices": [{"message": {"content": "Hello from Ollama"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                },
            }
        ).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, usage = _provider().generate("say hello")

        assert text == "Hello from Ollama"
        assert isinstance(usage, UsageMetadata)
        assert usage.prompt_tokens == 12
        assert usage.completion_tokens == 7
        assert usage.total_tokens == 19
        assert usage.cost_usd is None
        assert usage.model == "llama3.2"

    @patch("urllib.request.urlopen")
    def test_generate_posts_to_chat_completions(self, mock_urlopen):
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        provider = _provider(model="custom/model")
        provider.generate("diagnose this")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == f"{BASE_URL}/chat/completions"
        assert req.get_method() == "POST"
        body = json.loads(req.data)
        assert body["model"] == "custom/model"
        assert body["messages"] == [{"role": "user", "content": "diagnose this"}]

    @patch("urllib.request.urlopen")
    def test_generate_no_auth_header_when_token_empty(self, mock_urlopen):
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        _provider().generate("test")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None

    @patch("urllib.request.urlopen")
    def test_generate_missing_usage_returns_zeros(self, mock_urlopen):
        payload = json.dumps({"choices": [{"message": {"content": "Hello"}}]}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, usage = _provider().generate("say hello")

        assert text == "Hello"
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.cost_usd is None

    @patch("urllib.request.urlopen")
    def test_generate_null_content_returns_empty_string(self, mock_urlopen):
        """Tool-call responses carry content: null; that must not become 'None'."""
        payload = json.dumps({"choices": [{"message": {"content": None}}]}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        text, _ = _provider().generate("test")
        assert text == ""

    @patch("urllib.request.urlopen")
    def test_generate_no_choices_raises(self, mock_urlopen):
        payload = json.dumps({"choices": []}).encode()
        mock_urlopen.return_value = _mock_urlopen_response(payload)

        with pytest.raises(RuntimeError, match="no choices"):
            _provider().generate("test")

    @patch("urllib.request.urlopen")
    def test_generate_http_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(400)
        with pytest.raises(urllib.error.HTTPError):
            _provider().generate("test")

    @patch("urllib.request.urlopen")
    def test_generate_url_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection failed")
        with pytest.raises(urllib.error.URLError):
            _provider().generate("test")


class TestRemoteTimeouts:
    @patch("urllib.request.urlopen")
    def test_generate_waits_long_for_slow_local_models(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b'{"choices": [{"message": {"content": "ok"}}]}')
        _provider().generate("prompt")
        assert mock_urlopen.call_args.kwargs["timeout"] >= 300

    @patch("urllib.request.urlopen")
    def test_check_stays_short(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(b"{}")
        _provider().check()
        assert mock_urlopen.call_args.kwargs["timeout"] == 15

    @patch("urllib.request.urlopen")
    def test_hosted_provider_keeps_the_short_generate_timeout(self, mock_urlopen):
        from jaime.providers.openrouter import OpenRouterProvider

        mock_urlopen.return_value = _mock_urlopen_response(b'{"choices": [{"message": {"content": "ok"}}]}')
        OpenRouterProvider("tok").generate("prompt")
        assert mock_urlopen.call_args.kwargs["timeout"] == 60
