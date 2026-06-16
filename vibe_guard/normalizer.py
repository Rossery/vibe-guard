"""Normalizer: requirements / README -> discrete RequirementSpec.

Asks the LLM to decompose free-form documentation into a numbered list of
discrete, testable feature points. Each point is meant to be independently
verifiable against the code in Route A.
"""

from __future__ import annotations

from .llm import LLMClient
from .models import FeaturePoint, RepoGraph, RequirementSpec

SYSTEM = """You are a requirements analyst for a code-verification tool called Vibe Guard.
Given a project's README and/or a user-provided requirements description, decompose it into
a list of DISCRETE, TESTABLE functional feature points — the things the code is supposed to do.

Rules:
- Each feature point must be atomic (one capability), concrete, and verifiable against code.
- Prefer user-visible behaviour (CLI commands, API endpoints, data transforms, outputs).
- Ignore badges, license, contribution guides, installation boilerplate, and marketing fluff.
- 5 to 15 feature points is typical for a small/medium project; do not invent features.
- Respond with STRICT JSON only.
"""

USER_TMPL = """Project name (guess if unknown): {name}

=== README / DOCS ===
{readme}

=== EXTRA USER REQUIREMENTS (may be empty) ===
{user_req}

=== CODE SURFACE HINTS (top-level symbols, for grounding only) ===
{hints}

Return JSON of this exact shape:
{{
  "project_name": "<string>",
  "summary": "<one or two sentence summary of what the project does>",
  "feature_points": [
    {{
      "id": "F1",
      "title": "<short title>",
      "description": "<what it must do, concretely>",
      "category": "functional|cli|api|data|config|other",
      "priority": "must|should|could"
    }}
  ]
}}
"""


def _hints(repo: RepoGraph, limit: int = 60) -> str:
    lines: list[str] = []
    for s in repo.symbols:
        if s.kind == "class":
            lines.append(f"class {s.name} ({s.file})")
        elif s.kind == "function" and not s.name.startswith("_"):
            lines.append(f"def {s.signature} ({s.file})")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "(no top-level symbols extracted)"


def normalize(
    repo: RepoGraph, llm: LLMClient, user_requirements: str = ""
) -> RequirementSpec:
    readme = repo.readme.strip()
    name_guess = repo.root.rstrip("/").split("/")[-1]

    if not readme and not user_requirements.strip():
        # Nothing to normalize from; derive a minimal spec from code surface.
        return RequirementSpec(
            project_name=name_guess,
            summary="No README or requirements provided; spec derived is empty.",
            feature_points=[],
            source="none",
        )

    prompt = USER_TMPL.format(
        name=name_guess,
        readme=readme[:12000] or "(none)",
        user_req=user_requirements.strip()[:4000] or "(none)",
        hints=_hints(repo),
    )
    data = llm.chat_json(SYSTEM, prompt, max_tokens=3000)

    points: list[FeaturePoint] = []
    for i, fp in enumerate(data.get("feature_points", []), start=1):
        points.append(
            FeaturePoint(
                id=str(fp.get("id") or f"F{i}"),
                title=str(fp.get("title", "")).strip()[:160],
                description=str(fp.get("description", "")).strip()[:600],
                category=str(fp.get("category", "functional")).strip() or "functional",
                priority=str(fp.get("priority", "should")).strip() or "should",
            )
        )

    src = "readme" if readme else ""
    if user_requirements.strip():
        src = (src + "+user") if src else "user"
    return RequirementSpec(
        project_name=str(data.get("project_name") or name_guess),
        summary=str(data.get("summary", "")).strip(),
        feature_points=points,
        source=src or "readme",
    )
