"""Local caching for registry lookups.

Registry metadata barely changes between runs, so caching avoids hammering
PyPI/npm and makes CI runs fast and offline-friendly. Three implementations are
provided:

* :class:`MemoryCache`  — in-process dict (used in tests / one-shot runs).
* :class:`JsonFileCache` — TTL'd JSON file on disk (the default).
* :class:`NullCache`     — disables caching.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Optional, Protocol


class Cache(Protocol):
    """Minimal cache protocol."""

    def get(self, key: str) -> Optional[Any]:  # pragma: no cover - protocol
        ...

    def set(self, key: str, value: Any) -> None:  # pragma: no cover - protocol
        ...


class NullCache:
    """A cache that stores nothing (always a miss)."""

    def get(self, key: str) -> Optional[Any]:
        return None

    def set(self, key: str, value: Any) -> None:
        return None


class MemoryCache:
    """In-memory cache with optional TTL.

    ``now`` is injectable so tests can simulate the passage of time without
    sleeping.
    """

    def __init__(self, ttl_seconds: int = 0, now=time.time) -> None:
        self.ttl = ttl_seconds
        self._now = now
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if item is None:
            return None
        ts, value = item
        if self.ttl and (self._now() - ts) > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (self._now(), value)


class JsonFileCache:
    """Persistent JSON cache with TTL and atomic writes.

    The whole cache lives in a single JSON file. Entries are
    ``{"ts": <epoch>, "value": <json>}``. Expired entries are dropped lazily on
    read. Writes are atomic (temp file + ``os.replace``) so a crashed run never
    corrupts the cache.
    """

    def __init__(self, path: str, ttl_seconds: int = 24 * 3600, now=time.time) -> None:
        self.path = path
        self.ttl = ttl_seconds
        self._now = now
        self._store: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # -- internal ------------------------------------------------------- #
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._store = data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._store = {}

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._store, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- public --------------------------------------------------------- #
    def get(self, key: str) -> Optional[Any]:
        self._load()
        item = self._store.get(key)
        if item is None:
            return None
        ts = float(item.get("ts", 0))
        if self.ttl and (self._now() - ts) > self.ttl:
            self._store.pop(key, None)
            return None
        return item.get("value")

    def set(self, key: str, value: Any) -> None:
        self._load()
        self._store[key] = {"ts": self._now(), "value": value}
        self._flush()


def build_cache(use_cache: bool, cache_dir: str, ttl_seconds: int) -> Cache:
    """Factory: pick the right cache implementation for a config."""
    if not use_cache:
        return NullCache()
    return JsonFileCache(os.path.join(cache_dir, "registry.json"), ttl_seconds)
