"""AI provider interface and the HTTP plumbing shared by the providers."""

import json
import logging
import traceback
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from jaime.incident import UsageMetadata

logger = logging.getLogger(__name__)

_ERROR_BODY_MAX = 500


class AIProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> tuple[str, UsageMetadata]:
        """Generate a response for the given prompt.

        Returns a tuple of (response_text, usage_metadata).
        """
        ...

    def check(self) -> str | None:
        """Lightweight connectivity check.

        Returns None on success, or an error message string on failure.
        """
        return None


class HTTPProvider(AIProvider):
    """Provider that talks JSON over HTTP with the standard library only.

    Subclasses set ``_label`` (used in log and error messages) and implement
    ``check`` and ``generate`` on top of ``_check_url`` and ``_post_json``.
    """

    _label = "provider"
    _check_timeout_seconds = 15
    _generate_timeout_seconds = 60

    def _check_url(self, url: str, headers: dict[str, str]) -> str | None:
        """GET ``url``; None on success, an error message otherwise."""
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._check_timeout_seconds) as resp:
                resp.read()
            return None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:_ERROR_BODY_MAX]
            logger.warning("%s check failed:\n%s", self._label, traceback.format_exc())
            return f"{self._label} API HTTP {e.code}: {body}"
        except Exception as e:
            logger.warning("%s check failed:\n%s", self._label, traceback.format_exc())
            return f"{self._label} connection error: {e}"

    def _post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        """POST ``payload`` as JSON and return the decoded JSON response.

        HTTP and connection errors are logged and re-raised for the caller.
        """
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._generate_timeout_seconds) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:_ERROR_BODY_MAX]
            logger.error("%s API HTTP %s: %s", self._label, e.code, body)
            raise
        except urllib.error.URLError as e:
            logger.error("%s API connection error: %s", self._label, e.reason)
            raise


class OpenAIChatProvider(HTTPProvider):
    """Provider for an OpenAI Chat Completions endpoint.

    Subclasses supply the endpoint root (``_base_url``), the auth headers and
    the connectivity-check URL.
    """

    def __init__(self, api_token: str, model: str, base_url: str):
        self._api_token = api_token
        self._model = model
        self._base_url = base_url.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    def generate(self, prompt: str) -> tuple[str, UsageMetadata]:
        result = self._post_json(
            f"{self._base_url}/chat/completions",
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
            },
            self._auth_headers(),
        )

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"{self._label} returned no choices: {result}")

        # content can be None for tool-call responses; default to empty string.
        text = choices[0].get("message", {}).get("content") or ""

        # OpenAI-format usage. ``cost`` is read when the server reports it
        # (OpenRouter does; most local servers do not), otherwise left unset.
        raw_usage = result.get("usage", {})
        usage = UsageMetadata(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
            cost_usd=raw_usage.get("cost"),
            model=self._model,
        )
        return text, usage
