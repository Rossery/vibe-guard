"""Tests for the cache implementations."""

from __future__ import annotations

from pathlib import Path

from vibe_guard.hallucheck.cache import (
    JsonFileCache,
    MemoryCache,
    NullCache,
    build_cache,
)


def test_null_cache_never_stores():
    c = NullCache()
    c.set("k", {"v": 1})
    assert c.get("k") is None


def test_memory_cache_roundtrip():
    c = MemoryCache()
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert c.get("missing") is None


def test_memory_cache_ttl_expiry():
    clock = {"t": 1000.0}
    c = MemoryCache(ttl_seconds=10, now=lambda: clock["t"])
    c.set("k", 42)
    assert c.get("k") == 42
    clock["t"] = 1005.0
    assert c.get("k") == 42      # still fresh
    clock["t"] = 1011.0
    assert c.get("k") is None    # expired


def test_json_file_cache_persists(tmp_path: Path):
    path = str(tmp_path / "cache.json")
    c1 = JsonFileCache(path)
    c1.set("pypi:requests", {"exists": True})
    # a brand new instance reads the same file back
    c2 = JsonFileCache(path)
    assert c2.get("pypi:requests") == {"exists": True}


def test_json_file_cache_ttl(tmp_path: Path):
    clock = {"t": 0.0}
    path = str(tmp_path / "cache.json")
    c = JsonFileCache(path, ttl_seconds=100, now=lambda: clock["t"])
    c.set("k", 1)
    clock["t"] = 50
    assert c.get("k") == 1
    clock["t"] = 200
    assert c.get("k") is None


def test_json_file_cache_handles_corrupt_file(tmp_path: Path):
    path = tmp_path / "cache.json"
    path.write_text("{not valid json", encoding="utf-8")
    c = JsonFileCache(str(path))
    assert c.get("anything") is None  # degrades gracefully
    c.set("k", 1)
    assert c.get("k") == 1


def test_build_cache_factory(tmp_path: Path):
    assert isinstance(build_cache(False, str(tmp_path), 100), NullCache)
    assert isinstance(build_cache(True, str(tmp_path), 100), JsonFileCache)
