"""Unit tests for jaime.timeutil."""

import datetime

import pytest

from jaime.timeutil import UTC, parse_ts


class TestParseTs:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2026-07-14T09:37:54Z", datetime.datetime(2026, 7, 14, 9, 37, 54, tzinfo=UTC)),
            ("2026-07-14T09:37:54.123456789Z", datetime.datetime(2026, 7, 14, 9, 37, 54, 123456, tzinfo=UTC)),
            ("2026-07-14T09:37:54.12Z", datetime.datetime(2026, 7, 14, 9, 37, 54, 120000, tzinfo=UTC)),
            ("2026-07-14T09:37:54+00:00", datetime.datetime(2026, 7, 14, 9, 37, 54, tzinfo=UTC)),
            ("2026-07-14T11:37:54+02:00", datetime.datetime(2026, 7, 14, 9, 37, 54, tzinfo=UTC)),
        ],
    )
    def test_juju_and_iso_formats(self, value, expected):
        parsed = parse_ts(value)
        assert parsed == expected
        assert parsed.tzinfo == UTC

    def test_naive_value_is_taken_as_utc(self):
        assert parse_ts("2026-07-14T09:37:54") == datetime.datetime(2026, 7, 14, 9, 37, 54, tzinfo=UTC)

    def test_datetime_is_normalised(self):
        naive = datetime.datetime(2026, 7, 14, 9, 37, 54)
        assert parse_ts(naive).tzinfo == UTC

    @pytest.mark.parametrize("value", ["", None, "not a date", "2026-13-45T00:00:00Z"])
    def test_invalid_or_empty_returns_none(self, value):
        assert parse_ts(value) is None
