"""Tests for the PyPI / npm registry clients using a stub fetcher."""

from __future__ import annotations

import json

from vibe_guard.hallucheck.cache import MemoryCache
from vibe_guard.hallucheck.models import Ecosystem
from vibe_guard.hallucheck.registry import NpmClient, PyPIClient


def make_fetcher(responses):
    """Build a fetcher from a {url_substring: (status, body)} map.

    Records call count so cache behaviour can be asserted.
    """
    calls = {"n": 0}

    def fetch(url, timeout):
        calls["n"] += 1
        for frag, (status, body) in responses.items():
            if frag in url:
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                return status, body
        return 404, b""

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_pypi_existing_package():
    body = {
        "info": {"version": "2.31.0"},
        "releases": {
            "2.31.0": [{"upload_time_iso_8601": "2023-05-22T15:12:00Z"}],
            "2.0.0": [{"upload_time_iso_8601": "2019-01-01T00:00:00Z"}],
        },
    }
    client = PyPIClient(fetcher=make_fetcher({"/pypi/requests/": (200, body)}))
    meta = client.lookup("requests")
    assert meta is not None and meta.exists
    assert meta.latest_version == "2.31.0"
    # earliest release across all versions wins
    assert meta.first_release.startswith("2019-01-01")


def test_pypi_missing_package_is_404():
    client = PyPIClient(fetcher=make_fetcher({}))  # everything 404s
    meta = client.lookup("totallyfakepkg")
    assert meta is not None and meta.exists is False


def test_pypi_transient_error_returns_none():
    client = PyPIClient(fetcher=make_fetcher({"/pypi/x/": (503, b"")}))
    assert client.lookup("x") is None
    assert client.errors == 1


def test_registry_uses_cache():
    fetch = make_fetcher({"/pypi/requests/": (200, {"info": {}, "releases": {}})})
    client = PyPIClient(fetcher=fetch, cache=MemoryCache())
    client.lookup("requests")
    client.lookup("requests")
    assert fetch.calls["n"] == 1     # second hit served from cache
    assert client.cache_hits == 1


def test_npm_existing_with_age():
    body = {
        "dist-tags": {"latest": "1.2.3"},
        "time": {"created": "2025-12-01T00:00:00.000Z", "1.2.3": "2025-12-02T00:00:00.000Z"},
    }
    client = NpmClient(fetcher=make_fetcher({"registry.npmjs.org/leftpad": (200, body)}))
    meta = client.lookup("leftpad")
    assert meta is not None and meta.exists
    assert meta.latest_version == "1.2.3"
    assert meta.first_release.startswith("2025-12-01")


def test_npm_weekly_downloads():
    fetch = make_fetcher({
        "registry.npmjs.org/foo": (200, {"dist-tags": {}, "time": {}}),
        "downloads/point/last-week/foo": (200, {"downloads": 7}),
    })
    client = NpmClient(fetcher=fetch)
    assert client.fetch_weekly_downloads("foo") == 7


def test_age_days_computation():
    from datetime import datetime, timezone
    from vibe_guard.hallucheck.registry import PackageMetadata
    meta = PackageMetadata(name="x", ecosystem=Ecosystem.PYPI, exists=True,
                           first_release="2025-01-01T00:00:00+00:00")
    now = datetime(2025, 1, 31, tzinfo=timezone.utc)
    assert meta.age_days(now) == 30
