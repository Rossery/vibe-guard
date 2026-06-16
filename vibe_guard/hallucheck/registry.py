"""Registry clients for PyPI and npm.

Each client answers one question per package: *does it exist, and what does its
metadata say?* The HTTP layer is injectable (``fetcher``) so the whole module is
unit-testable offline, and every lookup is cached.

A lookup returns:

* a :class:`PackageMetadata` with ``exists=True/False`` on a definitive answer
  (200 / 404), or
* ``None`` on a transient error (timeout, 5xx, rate-limit) so callers can treat
  "unknown" differently from "confirmed missing".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .cache import Cache, NullCache
from .models import Ecosystem

# A fetcher returns (status_code, body_bytes). status 0 signals a transport
# error (no HTTP response at all).
Fetcher = Callable[[str, float], "tuple[int, bytes]"]


def urllib_fetcher(url: str, timeout: float) -> tuple[int, bytes]:
    """Default fetcher built on the stdlib (no third-party deps)."""
    req = urllib.request.Request(url, headers={"User-Agent": "vibe-guard-hallucheck/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001 - timeout / DNS / TLS → transport error
        return 0, b""


@dataclass
class PackageMetadata:
    """Normalised, ecosystem-agnostic view of a package's registry record."""

    name: str
    ecosystem: Ecosystem
    exists: bool
    first_release: Optional[str] = None  #: ISO-8601 date of earliest release
    latest_version: Optional[str] = None
    weekly_downloads: Optional[int] = None

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        """Days since the first release, or ``None`` if unknown."""
        if not self.first_release:
            return None
        try:
            dt = datetime.fromisoformat(self.first_release.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        return max(0, (ref - dt).days)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ecosystem": self.ecosystem.value,
            "exists": self.exists,
            "first_release": self.first_release,
            "latest_version": self.latest_version,
            "weekly_downloads": self.weekly_downloads,
        }


class RegistryClient(ABC):
    """Base class wiring cache + fetcher + parsing together."""

    ecosystem: Ecosystem

    def __init__(self, fetcher: Fetcher = urllib_fetcher,
                 cache: Optional[Cache] = None, timeout: float = 8.0) -> None:
        self.fetcher = fetcher
        self.cache: Cache = cache if cache is not None else NullCache()
        self.timeout = timeout
        self.cache_hits = 0
        self.errors = 0

    @abstractmethod
    def _metadata_url(self, name: str) -> str: ...

    @abstractmethod
    def _parse(self, name: str, status: int, body: bytes) -> Optional[PackageMetadata]:
        """Turn an HTTP response into metadata, or ``None`` if transient."""

    def lookup(self, name: str) -> Optional[PackageMetadata]:
        """Look *name* up (cached). ``None`` == transient/unknown."""
        key = f"{self.ecosystem.value}:{name.lower()}"
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return PackageMetadata(
                name=cached["name"], ecosystem=Ecosystem(cached["ecosystem"]),
                exists=cached["exists"], first_release=cached.get("first_release"),
                latest_version=cached.get("latest_version"),
                weekly_downloads=cached.get("weekly_downloads"),
            )
        status, body = self.fetcher(self._metadata_url(name), self.timeout)
        meta = self._parse(name, status, body)
        if meta is None:
            self.errors += 1
            return None
        self.cache.set(key, meta.to_dict())
        return meta


class PyPIClient(RegistryClient):
    """Client for the PyPI JSON API (``/pypi/<name>/json``)."""

    ecosystem = Ecosystem.PYPI

    def _metadata_url(self, name: str) -> str:
        return f"https://pypi.org/pypi/{name}/json"

    def _parse(self, name: str, status: int, body: bytes) -> Optional[PackageMetadata]:
        if status == 404:
            return PackageMetadata(name=name, ecosystem=self.ecosystem, exists=False)
        if status != 200 or not body:
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        info = data.get("info", {}) or {}
        first = _earliest_upload(data.get("releases", {}) or {})
        return PackageMetadata(
            name=name, ecosystem=self.ecosystem, exists=True,
            first_release=first, latest_version=info.get("version"),
        )


class NpmClient(RegistryClient):
    """Client for the npm registry (``registry.npmjs.org/<name>``).

    Download counts come from a second endpoint and are fetched lazily only when
    a freshness/popularity check needs them.
    """

    ecosystem = Ecosystem.NPM

    def _metadata_url(self, name: str) -> str:
        return f"https://registry.npmjs.org/{name}"

    def _downloads_url(self, name: str) -> str:
        return f"https://api.npmjs.org/downloads/point/last-week/{name}"

    def _parse(self, name: str, status: int, body: bytes) -> Optional[PackageMetadata]:
        if status == 404:
            return PackageMetadata(name=name, ecosystem=self.ecosystem, exists=False)
        if status != 200 or not body:
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        times = data.get("time", {}) or {}
        first = times.get("created")
        if first:
            first = _to_iso(first)
        dist_tags = data.get("dist-tags", {}) or {}
        return PackageMetadata(
            name=name, ecosystem=self.ecosystem, exists=True,
            first_release=first, latest_version=dist_tags.get("latest"),
        )

    def fetch_weekly_downloads(self, name: str) -> Optional[int]:
        """Best-effort weekly download count (``None`` on any error)."""
        status, body = self.fetcher(self._downloads_url(name), self.timeout)
        if status != 200 or not body:
            return None
        try:
            return int(json.loads(body).get("downloads"))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _to_iso(ts: str) -> Optional[str]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except (ValueError, AttributeError):
        return ts


def _earliest_upload(releases: dict) -> Optional[str]:
    """Earliest ``upload_time_iso_8601`` across all PyPI release files."""
    earliest: Optional[datetime] = None
    for files in releases.values():
        for f in files or []:
            ts = f.get("upload_time_iso_8601") or f.get("upload_time")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if earliest is None or dt < earliest:
                earliest = dt
    return earliest.isoformat() if earliest else None
