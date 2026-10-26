"""OpenRouter AI provider using the REST API directly (stdlib only)."""

from jaime.providers.base import OpenAIChatProvider

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIChatProvider):
    _label = "OpenRouter"

    def __init__(self, api_token: str, model: str = "~deepseek/deepseek-v4-flash-latest"):
        super().__init__(api_token, model, OPENROUTER_API_BASE)

    def check(self) -> str | None:
        """Lightweight connectivity check via auth key endpoint."""
        return self._check_url(f"{self._base_url}/auth/key", self._auth_headers())
