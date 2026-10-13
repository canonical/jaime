"""OpenAI-compatible remote AI provider using the REST API directly (stdlib only).

Unlike the hosted providers this one targets no fixed endpoint: the operator
points ``provider-remote-url`` at any server that speaks the OpenAI Chat
Completions protocol (Ollama, llama.cpp, vLLM, LiteLLM) and optionally supplies
an ``api-token``. When the token is empty no Authorization header is sent, so a
token-less local server works with zero secret configuration.
"""

import json
import logging
import traceback
import urllib.error
import urllib.request

from jaime.incident import UsageMetadata
from jaime.providers.base import AIProvider

logger = logging.getLogger(__name__)

_ERROR_BODY_MAX = 500


class RemoteProvider(AIProvider):
    def __init__(self, api_token: str = "", model: str = "", base_url: str = ""):
        self._api_token = api_token
        self._model = model
        self._base_url = base_url.rstrip("/")

    def check(self) -> str | None:
        """Lightweight connectivity check via the models endpoint."""
        url = f"{self._base_url}/models"
        req = urllib.request.Request(
            url,
            headers=self._auth_headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:_ERROR_BODY_MAX]
            logger.warning("Remote provider check failed:\n%s", traceback.format_exc())
            return f"remote provider API HTTP {e.code}: {body}"
        except Exception as e:
            logger.warning("Remote provider check failed:\n%s", traceback.format_exc())
            return f"remote provider connection error: {e}"

    def generate(self, prompt: str) -> tuple[str, UsageMetadata]:
        url = f"{self._base_url}/chat/completions"
        payload = json.dumps({
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_headers())

        req = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:_ERROR_BODY_MAX]
            logger.error("Remote provider API HTTP %s: %s", e.code, body)
            raise
        except urllib.error.URLError as e:
            logger.error("Remote provider API connection error: %s", e.reason)
            raise

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"remote provider returned no choices: {result}")

        message = choices[0].get("message", {})
        # content can be None for tool-call responses; default to empty string.
        text = message.get("content") or ""

        # Token usage in OpenAI format. cost is read if the server reports it
        # (most local servers do not), otherwise left unset.
        raw_usage = result.get("usage", {})
        cost = raw_usage.get("cost")
        usage = UsageMetadata(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
            cost_usd=cost,
            model=self._model,
        )

        return text, usage

    def _auth_headers(self) -> dict[str, str]:
        """Authorization header, present only when a token is configured."""
        if self._api_token:
            return {"Authorization": f"Bearer {self._api_token}"}
        return {}
