"""Unit tests for jaime.redact (secret redaction policy)."""

from jaime.redact import (
    REDACTED,
    is_secret_option,
    is_sensitive_name,
    redact_config_value,
    redact_text,
)


class TestIsSensitiveName:
    def test_sensitive_names(self):
        for name in (
            "password",
            "passwd",
            "secret",
            "token",
            "credential",
            "api-key",
            "api_key",
            "apiToken",
            "private_key",
            "secretKey",
            "accessKey",
            "juju-api-password",
            "POSTGRES_PASSWORD",
        ):
            assert is_sensitive_name(name), name

    def test_ordinary_names_are_not_sensitive(self):
        # The normalisation must not produce substring false positives.
        for name in (
            "monkey",
            "keyboard",
            "author",
            "port",
            "host",
            "name",
            "mode",
            "watch-applications",
            "api-url",
            "public-key",
        ):
            assert not is_sensitive_name(name), name

    def test_non_string_names(self):
        assert not is_sensitive_name(None)
        assert not is_sensitive_name("")


class TestRedactText:
    def test_known_secret_shapes(self):
        for text in (
            "binding to secret:abc123def",
            "Authorization: Bearer abcdefghijklmnop",
            "password=hunter2",
            "token: xyzzy",
            "api_key: AAAAAAAAAAAAAAAA",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----",
        ):
            assert redact_text(text) != text, text
            assert REDACTED in redact_text(text), text

    def test_ordinary_values_are_preserved(self):
        for text in (
            "commit 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a",
            "id 550e8400-e29b-41d4-a716-446655440000",
            "version 26.04 at /var/log/jaime/report.md",
            "listening on 0.0.0.0:5432",
            "ERROR: could not connect to local PostgreSQL",
        ):
            assert redact_text(text) == text, text

    def test_idempotent(self):
        once = redact_text("password=hunter2 and token: abcdefghijkl")
        assert redact_text(once) == once

    def test_none_and_empty(self):
        assert redact_text(None) == ""
        assert redact_text("") == ""


class TestRedactConfigValue:
    def test_sensitive_name_is_redacted(self):
        assert redact_config_value("password", "hunter2") == REDACTED
        assert redact_config_value("juju-api-password", "hunter2") == REDACTED

    def test_secret_typed_option_reports_set_or_unset(self):
        assert redact_config_value("token", "something", "secret") == f"{REDACTED} (set)"
        assert redact_config_value("token", "", "secret") == f"{REDACTED} (unset)"

    def test_secret_uri_value_is_redacted(self):
        assert redact_config_value("endpoint", "secret:abc123") == REDACTED

    def test_ordinary_option_is_scrubbed_but_returned(self):
        assert redact_config_value("port", "5432") == "5432"
        # A harmless name must not smuggle a recognisable secret through.
        assert redact_config_value("port", "token: xyzzy") == "token: [REDACTED]"

    def test_none_value(self):
        assert redact_config_value("port", None) == ""


class TestIsSecretOption:
    def test_true_for_name_type_or_uri(self):
        assert is_secret_option("password", "x")
        assert is_secret_option("token", "x", "secret")
        assert is_secret_option("endpoint", "secret:abc")
        assert not is_secret_option("port", "5432")
