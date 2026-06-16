"""Pydantic v2 data models shared across the Vibe Guard pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Ingest / symbol graph
# --------------------------------------------------------------------------- #
class Symbol(BaseModel):
    """A code symbol (function / class / method) discovered by tree-sitter."""

    name: str
    kind: str  # "function" | "class" | "method"
    file: str  # repo-relative path
    start_line: int
    end_line: int
    parent: Optional[str] = None  # enclosing class for methods
    signature: str = ""
    docstring: str = ""
    calls: list[str] = Field(default_factory=list)  # callee names referenced in body


class FileInfo(BaseModel):
    path: str
    language: str
    loc: int


class RepoGraph(BaseModel):
    """Result of the Ingest stage."""

    root: str
    files: list[FileInfo] = Field(default_factory=list)
    symbols: list[Symbol] = Field(default_factory=list)
    total_loc: int = 0
    readme: str = ""
    dependency_files: dict[str, str] = Field(default_factory=dict)  # path -> contents

    def symbols_by_file(self) -> dict[str, list[Symbol]]:
        out: dict[str, list[Symbol]] = {}
        for s in self.symbols:
            out.setdefault(s.file, []).append(s)
        return out


# --------------------------------------------------------------------------- #
# Normalizer -> RequirementSpec
# --------------------------------------------------------------------------- #
class FeaturePoint(BaseModel):
    """A single discrete, testable functional requirement."""

    id: str  # e.g. "F1"
    title: str
    description: str
    category: str = "functional"  # functional | cli | api | data | config | other
    priority: str = "should"  # must | should | could


class RequirementSpec(BaseModel):
    project_name: str = ""
    summary: str = ""
    feature_points: list[FeaturePoint] = Field(default_factory=list)
    source: str = ""  # "readme" | "user" | "readme+user"


# --------------------------------------------------------------------------- #
# Route A — feature alignment
# --------------------------------------------------------------------------- #
class AlignStatus(str, Enum):
    IMPLEMENTED = "implemented"
    PARTIAL = "partial"
    MISSING = "missing"
    UNCLEAR = "unclear"


class Evidence(BaseModel):
    file: str
    lines: str = ""  # e.g. "12-40"
    symbol: str = ""
    note: str = ""


class AlignmentResult(BaseModel):
    feature_id: str
    feature_title: str
    status: AlignStatus
    confidence: float = 0.0  # 0..1
    evidence: list[Evidence] = Field(default_factory=list)
    reasoning: str = ""


# --------------------------------------------------------------------------- #
# Route C — security / dependency findings
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SecurityFinding(BaseModel):
    tool: str  # semgrep | trivy | gitleaks | hallucinated-dep
    rule_id: str = ""
    title: str
    severity: Severity = Severity.MEDIUM
    file: str = ""
    line: int = 0
    detail: str = ""
    extra: dict = Field(default_factory=dict)


class ToolRun(BaseModel):
    tool: str
    available: bool
    ok: bool = False
    findings_count: int = 0
    error: str = ""
    duration_s: float = 0.0


# --------------------------------------------------------------------------- #
# Aggregate report
# --------------------------------------------------------------------------- #
class ScanReport(BaseModel):
    project_name: str
    root: str
    generated_at: str = ""
    repo: RepoGraph
    spec: RequirementSpec
    alignment: list[AlignmentResult] = Field(default_factory=list)
    security: list[SecurityFinding] = Field(default_factory=list)
    tool_runs: list[ToolRun] = Field(default_factory=list)

    # alignment rollup
    @property
    def align_counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in AlignStatus}
        for a in self.alignment:
            c[a.status.value] = c.get(a.status.value, 0) + 1
        return c

    @property
    def security_counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in Severity}
        for f in self.security:
            c[f.severity.value] = c.get(f.severity.value, 0) + 1
        return c
