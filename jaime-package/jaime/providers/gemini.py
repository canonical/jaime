"""Gemini AI provider using the REST API directly (stdlib only)."""

from jaime.incident import UsageMetadata
from jaime.providers.base import HTTPProvider

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(HTTPProvider):
    _label = "Gemini"

    def __init__(self, api_token: str, model: str = "gemini-2.0-flash"):
        self._api_token = api_token
        self._model = model

    def _auth_headers(self) -> dict[str, str]:
        # Token passed as a header to avoid exposure in URLs and proxy logs.
        return {"x-goog-api-key": self._api_token}

    def check(self) -> str | None:
        """Lightweight connectivity check via model list endpoint."""
        return self._check_url(GEMINI_API_BASE, self._auth_headers())

    def generate(self, prompt: str) -> tuple[str, UsageMetadata]:
        result = self._post_json(
            f"{GEMINI_API_BASE}/{self._model}:generateContent",
            {"contents": [{"parts": [{"text": prompt}]}]},
            self._auth_headers(),
        )

        candidates = result.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {result}")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise RuntimeError(f"Gemini response has no parts: {candidates[0]}")

        text = parts[0].get("text", "")

        # Extract token usage from usageMetadata if present.
        raw_usage = result.get("usageMetadata", {})
        usage = UsageMetadata(
            prompt_tokens=raw_usage.get("promptTokenCount", 0),
            completion_tokens=raw_usage.get("candidatesTokenCount", 0),
            total_tokens=raw_usage.get("totalTokenCount", 0),
            cost_usd=None,  # Gemini does not report cost in the response.
            model=self._model,
        )
        return text, usage
