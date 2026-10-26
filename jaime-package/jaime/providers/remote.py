"""OpenAI-compatible remote AI provider using the REST API directly (stdlib only).

Unlike the hosted providers this one targets no fixed endpoint: the operator
points ``provider-remote-url`` at any server that speaks the OpenAI Chat
Completions protocol (Ollama, llama.cpp, vLLM, LiteLLM) and optionally supplies
an ``api-token``. When the token is empty no Authorization header is sent, so a
token-less local server works with zero secret configuration.
"""

from jaime.providers.base import OpenAIChatProvider

# A local model on modest hardware can take minutes to answer a long report
# prompt, far longer than the hosted providers.
_GENERATE_TIMEOUT_SECONDS = 600


class RemoteProvider(OpenAIChatProvider):
    _label = "remote provider"
    _generate_timeout_seconds = _GENERATE_TIMEOUT_SECONDS

    def __init__(self, api_token: str = "", model: str = "", base_url: str = ""):
        super().__init__(api_token, model, base_url)

    def check(self) -> str | None:
        """Lightweight connectivity check via the models endpoint."""
        return self._check_url(f"{self._base_url}/models", self._auth_headers())

    def _auth_headers(self) -> dict[str, str]:
        """Authorization header, present only when a token is configured."""
        return super()._auth_headers() if self._api_token else {}
