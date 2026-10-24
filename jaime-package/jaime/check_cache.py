"""Short-lived cache of successful AI provider connectivity checks.

Each hook and action runs in a fresh process, so the cache lives on disk.
Only successes are cached: a failed check is always retried. The entry key is
a hash of the provider settings (never the token itself), so changing the
provider, model, URL or token invalidates it.
"""

import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

# How long a successful check stays valid.
CHECK_TTL_SECONDS = 300


class CheckCache:
    def __init__(self, path: str):
        self._path = path

    def is_fresh(self, key: str, now: datetime.datetime, ttl_seconds: int = CHECK_TTL_SECONDS) -> bool:
        """True when ``key`` passed a check less than ``ttl_seconds`` ago."""
        try:
            with open(self._path) as f:
                checked_at = datetime.datetime.fromisoformat(json.load(f)[key])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return 0 <= (now - checked_at).total_seconds() < ttl_seconds

    def record(self, key: str, now: datetime.datetime) -> None:
        """Remember that ``key`` passed a check at ``now`` (best effort)."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump({key: now.isoformat()}, f)
        except OSError as e:
            logger.debug("could not write provider check cache %s: %s", self._path, e)
