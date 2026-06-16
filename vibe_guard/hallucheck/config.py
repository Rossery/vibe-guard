"""Configuration for the hallucinated-dependency detector.

Severity is fully configurable per :class:`FindingKind` so a CI pipeline can,
for example, treat an *undeclared import* as ``LOW`` while keeping a
*declared-but-nonexistent* package at ``CRITICAL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import Ecosystem, FindingKind, Severity

# Default severity per finding kind. Hallucinated packages that would actually
# be installed are CRITICAL; name-similarity and freshness heuristics are
# advisory (HIGH/MEDIUM) because they carry false-positive risk.
_DEFAULT_SEVERITY: dict[FindingKind, Severity] = {
    FindingKind.DECLARED_NOT_FOUND: Severity.CRITICAL,
    FindingKind.IMPORTED_NOT_FOUND: Severity.CRITICAL,
    FindingKind.TYPOSQUAT: Severity.HIGH,
    FindingKind.SUSPICIOUS_NEW: Severity.MEDIUM,
    FindingKind.SUSPICIOUS_LOW_DOWNLOADS: Severity.LOW,
    FindingKind.UNDECLARED_IMPORT: Severity.MEDIUM,
}


def _default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "vibe-guard", "hallucheck")


@dataclass
class DetectorConfig:
    """Tunable knobs for a detection run."""

    # --- which ecosystems to scan --------------------------------------- #
    ecosystems: tuple[Ecosystem, ...] = (Ecosystem.PYPI, Ecosystem.NPM)

    # --- which checks to run -------------------------------------------- #
    check_registry: bool = True  #: query PyPI/npm for existence + metadata
    check_typosquat: bool = True  #: fuzzy-match against popular package names
    check_new_packages: bool = True  #: flag anomalously new packages
    check_undeclared: bool = True  #: report imports missing from manifests

    # --- thresholds ----------------------------------------------------- #
    new_package_max_age_days: int = 60  #: younger than this → SUSPICIOUS_NEW
    low_download_threshold: int = 500  #: weekly downloads below this → suspicious
    fuzzy_max_distance: int = 2  #: max edit distance for typo-squat matching

    # --- caching -------------------------------------------------------- #
    use_cache: bool = True
    cache_dir: str = field(default_factory=_default_cache_dir)
    cache_ttl_seconds: int = 24 * 3600  #: 1 day

    # --- network -------------------------------------------------------- #
    request_timeout: float = 8.0
    total_timeout: float = 120.0  #: overall budget for registry calls

    # --- severity overrides --------------------------------------------- #
    severity_overrides: dict[FindingKind, Severity] = field(default_factory=dict)

    def severity_for(self, kind: FindingKind) -> Severity:
        """Resolve the configured severity for *kind* (override → default)."""
        if kind in self.severity_overrides:
            return self.severity_overrides[kind]
        return _DEFAULT_SEVERITY[kind]
