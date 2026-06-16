"""``hallucheck`` — Vibe Guard's hallucinated-dependency detector (v0.2).

A self-contained, dependency-free module that detects AI-code supply-chain
failure modes:

* **hallucinated packages** — declared/imported names that do not exist on the
  registry (PyPI / npm) — the core *slopsquatting* risk;
* **typo-squats** — names one or two edits from a popular package;
* **suspiciously new packages** — recently-published planted packages;
* **undeclared imports** — used but never pinned in a manifest.

Quick start::

    from vibe_guard.hallucheck import HallucinationDetector
    result = HallucinationDetector().detect_path("path/to/project")
    print(result.to_json())
"""

from __future__ import annotations

from .config import DetectorConfig
from .detector import HallucinationDetector
from .models import (
    Dependency,
    DetectionResult,
    Ecosystem,
    Finding,
    FindingKind,
    Severity,
)

__all__ = [
    "HallucinationDetector",
    "DetectorConfig",
    "DetectionResult",
    "Finding",
    "FindingKind",
    "Severity",
    "Ecosystem",
    "Dependency",
]

__version__ = "0.2.0"
