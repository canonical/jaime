"""UTC time helpers shared by both charms.

Juju's controller API returns RFC 3339 timestamps with a ``Z`` suffix and up to
nine fractional digits (``2026-07-14T09:37:54.123456789Z``). Python 3.10, which
the 22.04 bases ship, cannot parse either with ``datetime.fromisoformat``, so
every timestamp Jaime reads goes through ``parse_ts``.
"""

import datetime
import re

UTC = datetime.timezone.utc

_FRACTION = re.compile(r"\.(\d+)")


def utc_now() -> datetime.datetime:
    """The current time, timezone-aware in UTC."""
    return datetime.datetime.now(UTC)


def utc_now_iso() -> str:
    """The current UTC time as an ISO 8601 string."""
    return utc_now().isoformat()


def parse_ts(value) -> datetime.datetime | None:
    """Parse an ISO 8601 / RFC 3339 timestamp into an aware UTC datetime.

    Accepts a ``Z`` suffix and any number of fractional digits (trimmed or
    padded to microseconds). A naive value is taken as UTC. Returns None for an
    empty or unparsable value; a ``datetime`` is normalised and returned.
    """
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        if not value:
            return None
        text = str(value).strip()
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
