"""OpenRouter AI provider using the REST API directly (stdlib only)."""

import json
import logging
import traceback
import urllib.error
import urllib.request

from jaime.incident import UsageMetadata
from jaime.providers.base import AIProvider

logger = logging.getLogger(__name__)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
_ERROR_BODY_MAX = 500


class OpenRouterProvider(AIProvider):
    def __init__(self, api_token: str, model: str = "~deepseek/deepseek-v4-flash-latest"):
        self._api_token = api_token
        self._model = model

    def check(self) -> str | None:
        """Lightweight connectivity check via auth key endpoint."""
        url = f"{OPENROUTER_API_BASE}/auth/key"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._api_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            logger.warning("OpenRouter check failed:\n%s", traceback.format_exc())
            return f"OpenRouter API HTTP {e.code}: {body[:200]}"
        except Exception as e:
            logger.warning("OpenRouter check failed:\n%s", traceback.format_exc())
            return f"OpenRouter connection error: {e}"

    def generate(self, prompt: str) -> tuple[str, UsageMetadata]:
        url = f"{OPENROUTER_API_BASE}/chat/completions"
        payload = json.dumps({
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:_ERROR_BODY_MAX]
            logger.error("OpenRouter API HTTP %s: %s", e.code, body)
            raise
        except urllib.error.URLError as e:
            logger.error("OpenRouter API connection error: %s", e.reason)
            raise

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {result}")

        message = choices[0].get("message", {})
        # content can be None for tool-call responses; default to empty string.
        text = message.get("content") or ""

        # Extract token usage. OpenRouter reports cost_usd directly.
        raw_usage = result.get("usage", {})
        usage = UsageMetadata(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
            cost_usd=raw_usage.get("cost"),
            model=self._model,
        )

        return text, usage
