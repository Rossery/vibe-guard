"""Standalone CLI for the hallucinated-dependency detector.

    vibe-guard-deps <path> [--json] [--ecosystem pypi,npm] [--no-registry] ...

Designed to run on its own (no LLM, no other Vibe Guard stages) so it slots
straight into CI. Exit code is non-zero when a finding at or above
``--fail-on`` severity is present, so a pipeline can gate on it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .config import DetectorConfig
from .detector import HallucinationDetector
from .models import Ecosystem, FindingKind, Severity

_SEV_ICON = {
    Severity.CRITICAL: "🛑", Severity.HIGH: "⛔", Severity.MEDIUM: "⚠️",
    Severity.LOW: "•", Severity.INFO: "·",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vibe-guard-deps",
        description="Detect hallucinated / typo-squatted / suspicious dependencies "
                    "in Python and JavaScript projects.",
    )
    p.add_argument("path", help="Path to the project to scan.")
    p.add_argument("--json", action="store_true", help="Emit JSON (for CI).")
    p.add_argument("--ecosystem", default="pypi,npm",
                   help="Comma-separated: pypi,npm (default: both).")
    p.add_argument("--no-registry", action="store_true",
                   help="Skip PyPI/npm network lookups (name heuristics only).")
    p.add_argument("--no-typosquat", action="store_true",
                   help="Disable fuzzy typo-squat matching.")
    p.add_argument("--no-new-check", action="store_true",
                   help="Disable the new-package freshness check.")
    p.add_argument("--no-cache", action="store_true", help="Disable the on-disk cache.")
    p.add_argument("--cache-dir", default=None, help="Override the cache directory.")
    p.add_argument("--new-age-days", type=int, default=None,
                   help="Flag packages younger than N days (default 60).")
    p.add_argument("--fuzzy-distance", type=int, default=None,
                   help="Max edit distance for typo-squat matching (default 2).")
    p.add_argument("--fail-on", default="high",
                   choices=[s.value for s in Severity],
                   help="Exit non-zero if a finding at/above this severity exists "
                        "(default: high).")
    return p


def _config_from_args(args) -> DetectorConfig:
    ecos = []
    for tok in args.ecosystem.split(","):
        tok = tok.strip().lower()
        if tok == "pypi":
            ecos.append(Ecosystem.PYPI)
        elif tok in ("npm", "js", "javascript"):
            ecos.append(Ecosystem.NPM)
    cfg = DetectorConfig(ecosystems=tuple(ecos) or (Ecosystem.PYPI, Ecosystem.NPM))
    cfg.check_registry = not args.no_registry
    cfg.check_typosquat = not args.no_typosquat
    cfg.check_new_packages = not args.no_new_check
    cfg.use_cache = not args.no_cache
    if args.cache_dir:
        cfg.cache_dir = args.cache_dir
    if args.new_age_days is not None:
        cfg.new_package_max_age_days = args.new_age_days
    if args.fuzzy_distance is not None:
        cfg.fuzzy_max_distance = args.fuzzy_distance
    return cfg


def _render_text(result, fail_on: Severity) -> str:
    lines: list[str] = []
    counts = result.severity_counts()
    lines.append("Vibe Guard · hallucinated-dependency scan")
    lines.append(
        f"  checked={result.checked_packages} cache_hits={result.cache_hits} "
        f"registry_errors={result.registry_errors}")
    lines.append(
        "  findings: "
        + " ".join(f"{s.value}={counts[s.value]}" for s in Severity if counts[s.value])
        + (" (none)" if not result.findings else ""))
    lines.append("")
    for f in result.findings:
        icon = _SEV_ICON[f.severity]
        where = f" [{f.source_file}]" if f.source_file else ""
        lines.append(f"{icon} {f.severity.value.upper():<8} {f.kind.value:<22} {f.message}{where}")
        if f.detail:
            lines.append(f"     ↳ {f.detail}")
        if f.suggestion:
            lines.append(f"     → {f.suggestion}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = _config_from_args(args)
    detector = HallucinationDetector(config=cfg)
    result = detector.detect_path(args.path)

    if args.json:
        print(result.to_json())
    else:
        print(_render_text(result, Severity(args.fail_on)))

    fail_on = Severity(args.fail_on)
    worst = result.max_severity()
    if worst is not None and worst.rank >= fail_on.rank:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
