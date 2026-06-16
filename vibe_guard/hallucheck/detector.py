"""The hallucinated-dependency detector — orchestrates parsers, registry
clients, fuzzy matching and config into a single :class:`DetectionResult`.

Detection logic per ecosystem:

1. Parse declared deps (manifests) and imported deps (source).
2. For each **declared** package: registry 404 → ``DECLARED_NOT_FOUND``;
   otherwise run typo-squat + freshness heuristics.
3. For each **imported-but-not-declared** package: registry 404 →
   ``IMPORTED_NOT_FOUND``; otherwise ``UNDECLARED_IMPORT`` (+ heuristics).

The registry clients are injectable, so the whole pipeline runs deterministically
offline in tests.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from . import fuzzy
from .cache import build_cache
from .config import DetectorConfig
from .models import (
    Dependency,
    DetectionResult,
    Ecosystem,
    Finding,
    FindingKind,
)
from .parsers import (
    collect_js_imports,
    collect_python_imports,
    parse_npm_manifests,
    parse_python_manifests,
)
from .registry import NpmClient, PyPIClient, RegistryClient


class HallucinationDetector:
    """Detect hallucinated / suspicious dependencies in a project tree."""

    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        pypi_client: Optional[RegistryClient] = None,
        npm_client: Optional[RegistryClient] = None,
        now: Optional[datetime] = None,
    ) -> None:
        self.config = config or DetectorConfig()
        cache = build_cache(self.config.use_cache, self.config.cache_dir,
                            self.config.cache_ttl_seconds)
        self.pypi = pypi_client or PyPIClient(cache=cache, timeout=self.config.request_timeout)
        self.npm = npm_client or NpmClient(cache=cache, timeout=self.config.request_timeout)
        self._now = now
        self._deadline = 0.0

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def detect_path(self, root: str) -> DetectionResult:
        """Scan the project rooted at *root* and return all findings."""
        self._deadline = time.monotonic() + self.config.total_timeout
        result = DetectionResult(ecosystems=list(self.config.ecosystems))

        if Ecosystem.PYPI in self.config.ecosystems:
            declared = parse_python_manifests(root)
            imported = collect_python_imports(root)
            self._scan_ecosystem(declared, imported, self.pypi, result)

        if Ecosystem.NPM in self.config.ecosystems:
            declared = parse_npm_manifests(root)
            imported = collect_js_imports(root)
            self._scan_ecosystem(declared, imported, self.npm, result)

        result.cache_hits = self.pypi.cache_hits + self.npm.cache_hits
        result.registry_errors = self.pypi.errors + self.npm.errors
        # stable, severity-first ordering for readable reports
        result.findings.sort(key=lambda f: (-f.severity.rank, f.ecosystem.value, f.package))
        return result

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _scan_ecosystem(
        self,
        declared: list[Dependency],
        imported: list[Dependency],
        client: RegistryClient,
        result: DetectionResult,
    ) -> None:
        declared_names = {d.name for d in declared}

        # 1) declared dependencies
        for dep in _dedupe(declared):
            self._check_dependency(dep, client, result, is_import=False)

        # 2) imports not covered by a declaration
        for dep in _dedupe(imported):
            if dep.name in declared_names:
                continue
            self._check_dependency(dep, client, result, is_import=True)

    def _check_dependency(
        self,
        dep: Dependency,
        client: RegistryClient,
        result: DetectionResult,
        is_import: bool,
    ) -> None:
        cfg = self.config

        # typo-squat is a name-only check — runs even without network.
        if cfg.check_typosquat:
            target = fuzzy.find_typosquat(dep.name, dep.ecosystem, cfg.fuzzy_max_distance)
            if target:
                result.findings.append(Finding(
                    kind=FindingKind.TYPOSQUAT, ecosystem=dep.ecosystem,
                    package=dep.name, severity=cfg.severity_for(FindingKind.TYPOSQUAT),
                    message=f"'{dep.name}' is suspiciously close to popular package '{target}'",
                    detail=f"Edit distance {fuzzy.levenshtein(dep.name, target)} from '{target}'. "
                           "Possible typo-squat / slopsquat.",
                    source_file=dep.source_file, suggestion=f"Did you mean '{target}'?",
                    extra={"target": target},
                ))

        if not cfg.check_registry:
            if is_import and cfg.check_undeclared:
                self._emit_undeclared(dep, result, exists=None)
            return

        meta = None
        if time.monotonic() < self._deadline:
            meta = client.lookup(dep.name)
            result.checked_packages += 1

        if meta is not None and not meta.exists:
            kind = FindingKind.IMPORTED_NOT_FOUND if is_import else FindingKind.DECLARED_NOT_FOUND
            where = "imported in source" if is_import else "declared in a manifest"
            result.findings.append(Finding(
                kind=kind, ecosystem=dep.ecosystem, package=dep.name,
                severity=cfg.severity_for(kind),
                message=f"{dep.ecosystem.value} package '{dep.name}' does not exist "
                        f"({where})",
                detail="Registry returned 404 — likely a hallucinated / slopsquatted "
                       "package. Do not install.",
                source_file=dep.source_file,
                extra={"import_name": dep.import_name},
            ))
            return

        # package exists (or unknown) → freshness + undeclared checks
        if meta is not None and meta.exists:
            self._freshness_checks(dep, meta, client, result)

        if is_import and cfg.check_undeclared:
            self._emit_undeclared(dep, result, exists=(meta.exists if meta else None))

    def _freshness_checks(self, dep, meta, client, result) -> None:
        cfg = self.config
        if cfg.check_new_packages:
            age = meta.age_days(self._now)
            if age is not None and age <= cfg.new_package_max_age_days:
                result.findings.append(Finding(
                    kind=FindingKind.SUSPICIOUS_NEW, ecosystem=dep.ecosystem,
                    package=dep.name,
                    severity=cfg.severity_for(FindingKind.SUSPICIOUS_NEW),
                    message=f"'{dep.name}' was first published only {age} day(s) ago",
                    detail=f"First release {meta.first_release}. Newly-registered "
                           "packages are a common malware/slopsquat vector — verify provenance.",
                    source_file=dep.source_file, extra={"age_days": age},
                ))
            # npm-only: low download signal (extra API call, best effort)
            if (dep.ecosystem is Ecosystem.NPM and isinstance(client, NpmClient)
                    and time.monotonic() < self._deadline):
                dl = client.fetch_weekly_downloads(dep.name)
                if dl is not None and dl < cfg.low_download_threshold:
                    result.findings.append(Finding(
                        kind=FindingKind.SUSPICIOUS_LOW_DOWNLOADS, ecosystem=dep.ecosystem,
                        package=dep.name,
                        severity=cfg.severity_for(FindingKind.SUSPICIOUS_LOW_DOWNLOADS),
                        message=f"'{dep.name}' has only {dl} weekly downloads",
                        detail="Very low adoption — double-check this is the intended package.",
                        source_file=dep.source_file, extra={"weekly_downloads": dl},
                    ))

    def _emit_undeclared(self, dep: Dependency, result: DetectionResult,
                         exists: Optional[bool]) -> None:
        low = dep.source_file.lower()
        is_dev = any(seg in low for seg in (
            "test", "tests/", "docs/", "doc/", "example", "examples",
            "conftest", "benchmark", "scripts/", "spec/", "__tests__"))
        from .models import Severity
        sev = Severity.INFO if is_dev else self.config.severity_for(FindingKind.UNDECLARED_IMPORT)
        confirmed = "" if exists is None else (
            " (confirmed on registry)" if exists else "")
        result.findings.append(Finding(
            kind=FindingKind.UNDECLARED_IMPORT, ecosystem=dep.ecosystem,
            package=dep.name, severity=sev,
            message=f"'{dep.name}' is imported but not declared in any manifest"
                    + (" (dev/test only)" if is_dev else ""),
            detail=f"`{dep.raw}` is used but '{dep.name}' is not pinned in a manifest"
                   + confirmed + ".",
            source_file=dep.source_file,
            extra={"import_name": dep.import_name, "registry_exists": exists},
        ))


def _dedupe(deps: list[Dependency]) -> list[Dependency]:
    """First occurrence wins, preserving order."""
    seen: set[str] = set()
    out: list[Dependency] = []
    for d in deps:
        if d.name in seen:
            continue
        seen.add(d.name)
        out.append(d)
    return out
