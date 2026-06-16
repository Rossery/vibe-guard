"""End-to-end detector tests with a fully stubbed registry (offline)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from vibe_guard.hallucheck.config import DetectorConfig
from vibe_guard.hallucheck.detector import HallucinationDetector
from vibe_guard.hallucheck.models import Ecosystem, FindingKind, Severity
from vibe_guard.hallucheck.registry import PackageMetadata, RegistryClient


class FakeClient(RegistryClient):
    """Registry client backed by an in-memory dict instead of the network."""

    def __init__(self, ecosystem: Ecosystem, table: dict[str, PackageMetadata | None]):
        super().__init__(fetcher=lambda u, t: (0, b""))
        self.ecosystem = ecosystem
        self._table = table

    def _metadata_url(self, name): return name
    def _parse(self, name, status, body): return None

    def lookup(self, name: str):
        if name in self._table:
            return self._table[name]
        # default: unknown names "exist" so we isolate the cases under test
        return PackageMetadata(name=name, ecosystem=self.ecosystem, exists=True,
                               first_release="2015-01-01T00:00:00+00:00")


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _kinds(result) -> dict[FindingKind, list]:
    out: dict[FindingKind, list] = {}
    for f in result.findings:
        out.setdefault(f.kind, []).append(f)
    return out


def _make(tmp_path: Path, table, **cfg_kwargs):
    cfg = DetectorConfig(ecosystems=(Ecosystem.PYPI,), use_cache=False, **cfg_kwargs)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake = FakeClient(Ecosystem.PYPI, table)
    det = HallucinationDetector(config=cfg, pypi_client=fake, npm_client=fake, now=now)
    return det.detect_path(str(tmp_path))


def test_declared_not_found_is_critical(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requests\nleftpad-hallucinated\n")
    table = {
        "requests": PackageMetadata("requests", Ecosystem.PYPI, exists=True,
                                    first_release="2011-01-01T00:00:00+00:00"),
        "leftpad-hallucinated": PackageMetadata("leftpad-hallucinated", Ecosystem.PYPI,
                                                exists=False),
    }
    result = _make(tmp_path, table)
    kinds = _kinds(result)
    assert FindingKind.DECLARED_NOT_FOUND in kinds
    f = kinds[FindingKind.DECLARED_NOT_FOUND][0]
    assert f.package == "leftpad-hallucinated"
    assert f.severity is Severity.CRITICAL


def test_imported_not_found(tmp_path: Path):
    _write(tmp_path, "app.py", "import nonexistentpkg\n")
    table = {"nonexistentpkg": PackageMetadata("nonexistentpkg", Ecosystem.PYPI,
                                               exists=False)}
    result = _make(tmp_path, table)
    kinds = _kinds(result)
    assert FindingKind.IMPORTED_NOT_FOUND in kinds
    assert kinds[FindingKind.IMPORTED_NOT_FOUND][0].severity is Severity.CRITICAL


def test_undeclared_import_runtime_vs_dev(tmp_path: Path):
    _write(tmp_path, "app.py", "import coollib\n")
    _write(tmp_path, "tests/test_app.py", "import pytestplugin\n")
    result = _make(tmp_path, {})  # all unknown names "exist"
    kinds = _kinds(result)
    und = kinds[FindingKind.UNDECLARED_IMPORT]
    sev = {f.package: f.severity for f in und}
    assert sev["coollib"] is Severity.MEDIUM       # runtime import
    assert sev["pytestplugin"] is Severity.INFO     # dev/test import downgraded


def test_typosquat_flagged(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requets\n")
    # the squatter even exists on the registry — still flagged by name
    table = {"requets": PackageMetadata("requets", Ecosystem.PYPI, exists=True,
                                        first_release="2010-01-01T00:00:00+00:00")}
    result = _make(tmp_path, table)
    kinds = _kinds(result)
    assert FindingKind.TYPOSQUAT in kinds
    f = kinds[FindingKind.TYPOSQUAT][0]
    assert f.extra["target"] == "requests"
    assert f.severity is Severity.HIGH


def test_suspicious_new_package(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "shiny-new-lib\n")
    table = {"shiny-new-lib": PackageMetadata("shiny-new-lib", Ecosystem.PYPI, exists=True,
                                              first_release="2025-12-20T00:00:00+00:00")}
    # 'now' is 2026-01-01 → ~12 days old, under the 60-day threshold
    result = _make(tmp_path, table)
    kinds = _kinds(result)
    assert FindingKind.SUSPICIOUS_NEW in kinds
    assert kinds[FindingKind.SUSPICIOUS_NEW][0].extra["age_days"] <= 60


def test_declared_real_package_is_clean(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requests\n")
    table = {"requests": PackageMetadata("requests", Ecosystem.PYPI, exists=True,
                                         first_release="2011-01-01T00:00:00+00:00")}
    result = _make(tmp_path, table)
    assert result.findings == []
    assert result.checked_packages == 1


def test_severity_overrides(tmp_path: Path):
    _write(tmp_path, "app.py", "import coollib\n")
    result = _make(tmp_path, {},
                   severity_overrides={FindingKind.UNDECLARED_IMPORT: Severity.LOW})
    f = [x for x in result.findings if x.kind is FindingKind.UNDECLARED_IMPORT][0]
    assert f.severity is Severity.LOW


def test_no_registry_mode_still_does_typosquat(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "requets\n")
    result = _make(tmp_path, {}, check_registry=False)
    kinds = _kinds(result)
    assert FindingKind.TYPOSQUAT in kinds
    assert result.checked_packages == 0  # no network lookups happened


def test_result_json_serialisation(tmp_path: Path):
    _write(tmp_path, "requirements.txt", "leftpad-hallucinated\n")
    table = {"leftpad-hallucinated": PackageMetadata("leftpad-hallucinated",
                                                     Ecosystem.PYPI, exists=False)}
    result = _make(tmp_path, table)
    import json
    blob = json.loads(result.to_json())
    assert blob["summary"]["total_findings"] >= 1
    assert blob["findings"][0]["package"] == "leftpad-hallucinated"
