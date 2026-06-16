"""Data models for the hallucinated-dependency detector (``hallucheck``).

Everything here is a plain ``dataclass`` / ``Enum`` so the module has **zero
third-party dependencies** and can be vendored or run on its own. JSON
serialisation is built in for CI integration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Ecosystem(str, Enum):
    """Package ecosystem a dependency belongs to."""

    PYPI = "pypi"
    NPM = "npm"


class Severity(str, Enum):
    """Finding severity, ordered ``CRITICAL`` > ... > ``INFO``."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Numeric rank — higher means more severe (``CRITICAL`` == 4)."""
        return _SEVERITY_RANK[self]

    def __ge__(self, other: "Severity") -> bool:  # type: ignore[override]
        return self.rank >= other.rank

    def __gt__(self, other: "Severity") -> bool:  # type: ignore[override]
        return self.rank > other.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class FindingKind(str, Enum):
    """The class of dependency problem detected.

    The first three mirror the v0.1 ``security.py`` behaviour; the rest are new
    in v0.2.
    """

    #: Declared in a manifest but the registry returns 404 — classic
    #: hallucinated / slopsquatted package.
    DECLARED_NOT_FOUND = "declared_not_found"
    #: Imported in source, maps to a distribution that does not exist.
    IMPORTED_NOT_FOUND = "imported_not_found"
    #: Imported but never declared in any manifest (supply-chain hygiene).
    UNDECLARED_IMPORT = "undeclared_import"
    #: Name is one or two edits away from a very popular package — typo-squat.
    TYPOSQUAT = "typosquat"
    #: Package exists but was first published very recently — possibly planted.
    SUSPICIOUS_NEW = "suspicious_new"
    #: Package exists but has anomalously low download counts.
    SUSPICIOUS_LOW_DOWNLOADS = "suspicious_low_downloads"


@dataclass
class Dependency:
    """A dependency discovered either from a manifest or from an import."""

    name: str  #: normalised distribution / package name (lower-case)
    ecosystem: Ecosystem
    source_file: str  #: repo-relative path it was found in
    declared: bool  #: True if from a manifest, False if only imported
    raw: str = ""  #: original spec / import string
    import_name: Optional[str] = None  #: original import token, if import-derived

    def key(self) -> str:
        return f"{self.ecosystem.value}:{self.name}"


@dataclass
class Finding:
    """A single detector finding, carrying its own evidence."""

    kind: FindingKind
    ecosystem: Ecosystem
    package: str
    severity: Severity
    message: str
    detail: str = ""
    source_file: str = ""
    suggestion: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["ecosystem"] = self.ecosystem.value
        d["severity"] = self.severity.value
        return d


@dataclass
class DetectionResult:
    """Aggregate result of a detection run."""

    findings: list[Finding] = field(default_factory=list)
    checked_packages: int = 0
    cache_hits: int = 0
    registry_errors: int = 0
    ecosystems: list[Ecosystem] = field(default_factory=list)

    def severity_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity.value] += 1
        return counts

    def max_severity(self) -> Optional[Severity]:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total_findings": len(self.findings),
                "checked_packages": self.checked_packages,
                "cache_hits": self.cache_hits,
                "registry_errors": self.registry_errors,
                "ecosystems": [e.value for e in self.ecosystems],
                "severity_counts": self.severity_counts(),
                "max_severity": (self.max_severity() or Severity.INFO).value
                if self.findings else None,
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
