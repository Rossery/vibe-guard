"""Route C: security & dependency scanning.

Integrates external scanners (Semgrep, Trivy, Gitleaks) when available and adds
an in-house "hallucinated dependency" detector. The latter targets a failure
mode specific to AI-generated code: imports / declared requirements that point
at packages which do not actually exist on PyPI (a.k.a. "slopsquatting" bait),
or third-party imports that are used but never declared as dependencies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .models import RepoGraph, SecurityFinding, Severity, ToolRun

# ---- tool discovery ------------------------------------------------------- #
_EXTRA_BIN = os.path.expanduser("~/.local/bin")


def _which(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    cand = os.path.join(_EXTRA_BIN, name)
    return cand if os.path.exists(cand) else None


def _run(cmd: list[str], timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        env={**os.environ, "SEMGREP_SEND_METRICS": "off"},
    )


# --------------------------------------------------------------------------- #
# Semgrep
# --------------------------------------------------------------------------- #
_SEMGREP_SEV = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}


# Registry packs that work with metrics disabled (unlike "--config auto").
_SEMGREP_PACKS = ("p/python", "p/security-audit", "p/secrets")


def run_semgrep(root: str, configs: tuple[str, ...] = _SEMGREP_PACKS,
                timeout: int = 300) -> tuple[ToolRun, list[SecurityFinding]]:
    bin_ = _which("semgrep")
    t0 = time.time()
    if not bin_:
        return ToolRun(tool="semgrep", available=False, error="not installed"), []
    cfg_args: list[str] = []
    for c in configs:
        cfg_args += ["--config", c]
    try:
        proc = _run(
            [bin_, *cfg_args, "--json", "--quiet", "--timeout", "30",
             "--max-target-bytes", "1000000", root],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolRun(tool="semgrep", available=True, error="timeout",
                       duration_s=time.time() - t0), []
    except Exception as e:  # noqa: BLE001
        return ToolRun(tool="semgrep", available=True, error=str(e)[:200]), []

    findings: list[SecurityFinding] = []
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ToolRun(tool="semgrep", available=True, ok=False,
                       error=(proc.stderr or "bad json")[:200],
                       duration_s=time.time() - t0), []

    for r in data.get("results", []):
        meta = r.get("extra", {})
        sev = _SEMGREP_SEV.get(str(meta.get("severity", "WARNING")).upper(), Severity.MEDIUM)
        findings.append(SecurityFinding(
            tool="semgrep",
            rule_id=str(r.get("check_id", "")),
            title=str(meta.get("message", r.get("check_id", "")))[:200],
            severity=sev,
            file=os.path.relpath(r.get("path", ""), root) if r.get("path") else "",
            line=int(r.get("start", {}).get("line", 0) or 0),
            detail=str(meta.get("metadata", {}).get("category", "")),
        ))
    return ToolRun(tool="semgrep", available=True, ok=True,
                   findings_count=len(findings), duration_s=time.time() - t0), findings


# --------------------------------------------------------------------------- #
# Gitleaks
# --------------------------------------------------------------------------- #
def run_gitleaks(root: str, timeout: int = 120) -> tuple[ToolRun, list[SecurityFinding]]:
    bin_ = _which("gitleaks")
    t0 = time.time()
    if not bin_:
        return ToolRun(tool="gitleaks", available=False, error="not installed"), []
    out = os.path.join(root, ".vibe_gitleaks.json")
    try:
        _run([bin_, "detect", "--source", root, "--no-git",
              "--report-format", "json", "--report-path", out, "--redact", "--exit-code", "0"],
             timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolRun(tool="gitleaks", available=True, error="timeout",
                       duration_s=time.time() - t0), []
    except Exception as e:  # noqa: BLE001
        return ToolRun(tool="gitleaks", available=True, error=str(e)[:200]), []

    findings: list[SecurityFinding] = []
    try:
        with open(out) as f:
            data = json.load(f)
        for r in data:
            findings.append(SecurityFinding(
                tool="gitleaks",
                rule_id=str(r.get("RuleID", "")),
                title=f"Hardcoded secret: {r.get('Description', r.get('RuleID', ''))}"[:200],
                severity=Severity.HIGH,
                file=os.path.relpath(r.get("File", ""), root) if r.get("File") else "",
                line=int(r.get("StartLine", 0) or 0),
                detail=str(r.get("Match", ""))[:120],
            ))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        return ToolRun(tool="gitleaks", available=True, error=str(e)[:200],
                       duration_s=time.time() - t0), []
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return ToolRun(tool="gitleaks", available=True, ok=True,
                   findings_count=len(findings), duration_s=time.time() - t0), findings


# --------------------------------------------------------------------------- #
# Trivy (best effort — needs a vuln DB; optional)
# --------------------------------------------------------------------------- #
_TRIVY_SEV = {
    "CRITICAL": Severity.CRITICAL, "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW, "UNKNOWN": Severity.INFO,
}


def run_trivy(root: str, timeout: int = 300) -> tuple[ToolRun, list[SecurityFinding]]:
    bin_ = _which("trivy")
    t0 = time.time()
    if not bin_:
        return ToolRun(tool="trivy", available=False, error="not installed"), []
    try:
        proc = _run([bin_, "fs", "--scanners", "vuln,secret", "--format", "json",
                     "--quiet", "--timeout", "5m", root], timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolRun(tool="trivy", available=True, error="timeout",
                       duration_s=time.time() - t0), []
    except Exception as e:  # noqa: BLE001
        return ToolRun(tool="trivy", available=True, error=str(e)[:200]), []

    findings: list[SecurityFinding] = []
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ToolRun(tool="trivy", available=True, ok=False,
                       error=(proc.stderr or "bad json")[:200],
                       duration_s=time.time() - t0), []
    for res in data.get("Results", []) or []:
        target = res.get("Target", "")
        for v in res.get("Vulnerabilities", []) or []:
            findings.append(SecurityFinding(
                tool="trivy",
                rule_id=str(v.get("VulnerabilityID", "")),
                title=f"{v.get('PkgName','')}: {v.get('Title', v.get('VulnerabilityID',''))}"[:200],
                severity=_TRIVY_SEV.get(str(v.get("Severity", "UNKNOWN")).upper(), Severity.INFO),
                file=target,
                detail=f"{v.get('InstalledVersion','')} -> {v.get('FixedVersion','(no fix)')}",
            ))
        for s in res.get("Secrets", []) or []:
            findings.append(SecurityFinding(
                tool="trivy", rule_id=str(s.get("RuleID", "")),
                title=f"Secret: {s.get('Title','')}"[:200], severity=Severity.HIGH,
                file=target, line=int(s.get("StartLine", 0) or 0),
            ))
    return ToolRun(tool="trivy", available=True, ok=True,
                   findings_count=len(findings), duration_s=time.time() - t0), findings


# --------------------------------------------------------------------------- #
# Hallucinated dependency detector
# --------------------------------------------------------------------------- #
# As of v0.2 the detector lives in its own solid, independently-testable module
# (``vibe_guard.hallucheck``) with typo-squat matching, npm support, caching and
# a standalone CLI. ``run_hallucinated_deps`` below is a thin adapter that runs
# that module over the repo root and maps its findings back onto the pipeline's
# ``SecurityFinding`` shape — keeping Route C wiring unchanged.

# common import-name -> PyPI distribution name mismatches (kept for back-compat;
# the canonical table now lives in ``hallucheck.parsers.PYPI_IMPORT_ALIASES``).
_KNOWN_ALIASES = {
    "yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "dotenv": "python-dotenv", "jwt": "pyjwt",
    "dateutil": "python-dateutil", "git": "gitpython", "serial": "pyserial",
    "OpenSSL": "pyopenssl", "Crypto": "pycryptodome", "attr": "attrs", "google": "google-api-python-client",
}

# map hallucheck finding kinds -> (rule_id, SecurityFinding severity)
_HALLU_RULE = {
    "declared_not_found": ("VG-DEP-001", Severity.CRITICAL),
    "imported_not_found": ("VG-DEP-002", Severity.CRITICAL),
    "undeclared_import": ("VG-DEP-003", None),   # severity passed through
    "typosquat": ("VG-DEP-004", Severity.HIGH),
    "suspicious_new": ("VG-DEP-005", Severity.MEDIUM),
    "suspicious_low_downloads": ("VG-DEP-006", Severity.LOW),
}


def _local_module_names(repo: RepoGraph) -> set[str]:
    names: set[str] = set()
    for fi in repo.files:
        parts = Path(fi.path).parts
        if parts:
            names.add(Path(parts[0]).stem)
        names.add(Path(fi.path).stem)
    # any directory holding an __init__.py is a local package
    for fi in repo.files:
        if Path(fi.path).name == "__init__.py":
            p = Path(fi.path).parent
            if p.parts:
                names.add(p.parts[-1])
    return {n for n in names if n}


def _collect_imports(repo: RepoGraph) -> dict[str, str]:
    """top-level imported module -> first file where it appears.

    Handles ``import a, b as c`` and ``from x.y import z`` and skips relative
    imports (``from . import ...``)."""
    found: dict[str, str] = {}

    def add(mod: str, file: str) -> None:
        mod = mod.strip().split(".")[0].strip()
        if mod and mod.isidentifier() and not mod.startswith("_") and mod not in found:
            found[mod] = file

    for fi in repo.files:
        full = Path(repo.root) / fi.path
        try:
            text = full.read_text("utf-8", "replace")
        except Exception:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("from "):
                m = re.match(r"from\s+([A-Za-z0-9_\.]+)\s+import", line)
                if m and not m.group(1).startswith("."):
                    add(m.group(1), fi.path)
            elif line.startswith("import "):
                for seg in line[len("import "):].split(","):
                    name = seg.strip().split(" as ")[0]
                    add(name, fi.path)
    return found


def _dep_name(spec: str) -> str | None:
    """Extract the distribution name from a PEP 508 requirement spec."""
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    if not m:
        return None
    return m.group(1).lower().replace("_", "-")


def _parse_declared_deps(repo: RepoGraph) -> set[str]:
    """Parse *only* real dependency declarations — never arbitrary quoted
    strings (which produced false 'hallucinated package' findings in v0.1)."""
    import tomllib

    declared: set[str] = set()

    for path, content in repo.dependency_files.items():
        base = Path(path).name.lower()

        if base.endswith(".txt") or "requirements" in base or base == "constraints.txt":
            for line in content.splitlines():
                line = line.split("#")[0].strip()
                if not line or line.startswith(("-", "git+", "http", "//", ".")):
                    continue
                n = _dep_name(line)
                if n:
                    declared.add(n)

        elif base == "pyproject.toml":
            try:
                data = tomllib.loads(content)
            except Exception:
                continue
            proj = data.get("project", {}) or {}
            for d in proj.get("dependencies", []) or []:
                n = _dep_name(d)
                if n:
                    declared.add(n)
            for grp in (proj.get("optional-dependencies", {}) or {}).values():
                for d in grp or []:
                    n = _dep_name(d)
                    if n:
                        declared.add(n)
            for d in (data.get("build-system", {}) or {}).get("requires", []) or []:
                n = _dep_name(d)
                if n:
                    declared.add(n)
            # poetry-style
            for d in ((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("dependencies", {}) or {}:
                if d.lower() != "python":
                    declared.add(d.lower().replace("_", "-"))

        elif base == "setup.py":
            for block in re.findall(
                r"(?:install_requires|setup_requires|tests_require|extras_require)\s*=\s*([\[{].*?[\]}])",
                content, re.DOTALL,
            ):
                for s in re.findall(r"[\"']([^\"']+)[\"']", block):
                    n = _dep_name(s)
                    if n:
                        declared.add(n)

        elif base == "setup.cfg":
            m = re.search(r"install_requires\s*=\s*\n((?:\s+.+\n)+)", content)
            if m:
                for line in m.group(1).splitlines():
                    n = _dep_name(line.strip())
                    if n:
                        declared.add(n)

    return declared


_PYPI_CACHE: dict[str, bool] = {}


def _pypi_exists(pkg: str, timeout: float = 8.0) -> bool | None:
    key = pkg.lower()
    if key in _PYPI_CACHE:
        return _PYPI_CACHE[key]
    url = f"https://pypi.org/pypi/{pkg}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "vibe-guard/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status == 200
            _PYPI_CACHE[key] = ok
            return ok
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _PYPI_CACHE[key] = False
            return False
        return None  # transient / rate-limited -> unknown
    except Exception:
        return None  # network error -> unknown


def run_hallucinated_deps(repo: RepoGraph, check_pypi: bool = True,
                          timeout: int = 90) -> tuple[ToolRun, list[SecurityFinding]]:
    """Route C dependency check — adapter over ``vibe_guard.hallucheck``.

    Runs the standalone detector across the repo root and maps its richer
    findings (hallucinated / typo-squat / new-package / undeclared, PyPI + npm)
    back onto the pipeline's :class:`SecurityFinding` shape.
    """
    from .hallucheck import DetectorConfig, Ecosystem, HallucinationDetector, Severity as HSev

    t0 = time.time()
    cfg = DetectorConfig(
        ecosystems=(Ecosystem.PYPI, Ecosystem.NPM),
        check_registry=check_pypi,
        total_timeout=float(timeout),
    )
    try:
        result = HallucinationDetector(config=cfg).detect_path(repo.root)
    except Exception as e:  # noqa: BLE001 - never let Route C crash the pipeline
        return ToolRun(tool="hallucinated-dep", available=True, ok=False,
                       error=str(e)[:200], duration_s=time.time() - t0), []

    sev_map = {
        HSev.CRITICAL: Severity.CRITICAL, HSev.HIGH: Severity.HIGH,
        HSev.MEDIUM: Severity.MEDIUM, HSev.LOW: Severity.LOW, HSev.INFO: Severity.INFO,
    }
    findings: list[SecurityFinding] = []
    for f in result.findings:
        rule_id, fixed_sev = _HALLU_RULE.get(f.kind.value, ("VG-DEP-000", None))
        findings.append(SecurityFinding(
            tool="hallucinated-dep", rule_id=rule_id,
            title=f.message, severity=fixed_sev or sev_map[f.severity],
            file=f.source_file, detail=f.detail,
            extra={"kind": f.kind.value, "ecosystem": f.ecosystem.value,
                   "package": f.package, **f.extra},
        ))

    run = ToolRun(tool="hallucinated-dep", available=True, ok=True,
                  findings_count=len(findings), duration_s=time.time() - t0)
    run.error = "" if check_pypi else "registry check skipped"
    return run, findings


# --------------------------------------------------------------------------- #
def scan_security(repo: RepoGraph, check_pypi: bool = True,
                  use_trivy: bool = True) -> tuple[list[ToolRun], list[SecurityFinding]]:
    runs: list[ToolRun] = []
    findings: list[SecurityFinding] = []
    root = repo.root

    for fn in (run_semgrep, run_gitleaks):
        run, f = fn(root)
        runs.append(run)
        findings.extend(f)

    if use_trivy:
        run, f = run_trivy(root)
        runs.append(run)
        findings.extend(f)

    run, f = run_hallucinated_deps(repo, check_pypi=check_pypi)
    runs.append(run)
    findings.extend(f)

    return runs, findings
