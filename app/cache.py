"""
Minimal in-memory TTL cache, keyed by LinkedIn public profile id.

Why this matters here specifically: every /profile call hits LinkedIn's
live API using our one authenticated account. Repeated requests for the
same profile in a short window (dev testing, someone refreshing a page,
duplicate requests) cost nothing to serve from cache and meaningfully
reduce how often we hit LinkedIn — which is directly tied to how likely
the backing account is to get rate-limited or flagged.

Intentionally simple: single-process, in-memory, no external dependency
(Redis etc.) — sufficient for this service's scale, and easy to reason
about. Swap for Redis if this ever needs to run across multiple
processes/instances.
"""

import time
from threading import Lock


class TTLCache:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}
        self._lock = Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: dict):
        with self._lock:
            self._store[key] = (time.time() + self.ttl_seconds, value)

    def stats(self) -> dict:
        with self._lock:
            return {"cached_profiles": len(self._store), "ttl_seconds": self.ttl_seconds}
