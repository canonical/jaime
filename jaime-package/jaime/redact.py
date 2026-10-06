"""Redaction of secrets from incident reports.

The report is the persisted evidence artifact and the source of the suggest
prompt: ``core.py`` reads the report back from disk before calling the provider,
so redacting when the report is written cleans both. Collection is untouched;
the in-memory context is never persisted.

Two layers:

- Config values are redacted by option name and Juju type, because a secret with
  no recognisable shape (a random passphrase) is invisible to a pattern scan.
- Free text (logs, health-command output, snap logs, the status message) is
  scrubbed by conservative value patterns.

The policy and its non-goals are in ``ARCHITECTURE.md`` and
``specs/261006-feature-redact-secrets/``.
"""

import re

REDACTED = "[REDACTED]"

# Option names that carry secrets. Matching runs on a normalised name:
# lowercase, camelCase and separators split into tokens, plus the
# separators-removed form. That way `password`, `api-token`, `apiToken` and
# `api_token` match while `monkey`, `keyboard` and `author` do not.
_SENSITIVE_TOKENS = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
})
_SENSITIVE_JOINED = (
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Conservative value patterns: known secret shapes only. A generic
# high-entropy heuristic is deliberately absent, because it would redact commit
# SHAs, UUIDs and IDs and destroy evidence.
_PATTERNS = (
    # Juju secret URI, e.g. secret:abc123
    re.compile(r"secret:[0-9a-z-]+"),
    # Bearer token
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # PEM private key block
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    # JWT: three dot-separated base64url segments
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)

# `key: value` or `key=value` where the key is sensitive; only the value is
# replaced, so the log line stays readable.
_SENSITIVE_KEY = (
    r"(?:password|passwd|pass|secret|token|api[-_]?key|authorization|credential)"
)
_ASSIGNMENT = re.compile(r"(?i)(\b" + _SENSITIVE_KEY + r"\b\s*[:=]\s*)(\S+)")


def is_sensitive_name(name) -> bool:
    """Whether a config option name marks its value as sensitive."""
    if not isinstance(name, str) or not name:
        return False
    split = _CAMEL_BOUNDARY.sub(" ", name).lower()
    if set(_NON_ALNUM.split(split)) & _SENSITIVE_TOKENS:
        return True
    joined = _NON_ALNUM.sub("", split)
    return any(form in joined for form in _SENSITIVE_JOINED)


def is_secret_uri(value) -> bool:
    """Whether a value is a Juju secret URI reference."""
    return isinstance(value, str) and value.startswith("secret:")


def is_secret_option(name, value=None, option_type=None) -> bool:
    """Whether a config option's value must never be rendered."""
    return (
        is_sensitive_name(name)
        or option_type == "secret"
        or is_secret_uri(value)
    )


def _stringify(value) -> str:
    return "" if value is None else str(value)


def redact_config_value(name, value, option_type=None) -> str:
    """Return the safe rendering of a Juju config option value.

    A sensitive name, a ``secret`` type or a ``secret:`` URI yields the marker.
    A secret-typed option is marked set or unset, never by value. An ordinary
    option is still passed through the text scrub, so it cannot carry a
    recognisable secret under a harmless name.
    """
    if is_secret_option(name, value, option_type):
        if option_type == "secret":
            return f"{REDACTED} (set)" if value else f"{REDACTED} (unset)"
        return REDACTED
    return redact_text(_stringify(value))


def redact_text(text) -> str:
    """Replace recognisable secrets in free text with the marker.

    Idempotent: the marker matches none of the patterns, and a second pass over
    already-redacted text is byte-identical.
    """
    if text is None:
        return ""
    out = str(text)
    for pattern in _PATTERNS:
        out = pattern.sub(REDACTED, out)
    return _ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, out)
