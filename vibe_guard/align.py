"""Route A: feature alignment.

For each feature point we (1) retrieve the most relevant symbols from the repo
graph using lightweight keyword overlap, (2) pull their source snippets, and
(3) ask the LLM to judge whether the feature is implemented, citing file:line
evidence. Keeping the LLM grounded in real, located snippets is what lets it
produce trustworthy `file:line` evidence rather than hallucinated references.
"""

from __future__ import annotations

import re
from pathlib import Path

from .llm import LLMClient
from .models import (
    AlignmentResult,
    AlignStatus,
    Evidence,
    FeaturePoint,
    RepoGraph,
    Symbol,
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "must", "should",
    "could", "able", "user", "data", "file", "files", "code", "value", "values",
    "support", "provide", "allow", "given", "into", "each", "when", "name",
}


def _tokens(text: str) -> set[str]:
    out = set()
    for m in _WORD.findall(text.lower()):
        if len(m) >= 3 and m not in _STOP:
            out.add(m)
            # split snake_case/camelCase-ish into parts too
            for part in re.split(r"_", m):
                if len(part) >= 3 and part not in _STOP:
                    out.add(part)
    return out


def _symbol_tokens(s: Symbol) -> set[str]:
    return _tokens(f"{s.name} {s.signature} {s.docstring} {s.file} {' '.join(s.calls)}")


# Symbol names that frequently embody entry points / wiring.
_ENTRY_NAMES = {"main", "cli", "app", "run", "handler", "serve", "execute",
                "dispatch", "command", "entrypoint", "start"}
_ENTRY_CATEGORIES = {"cli", "api", "config"}


def _retrieve(fp: FeaturePoint, repo: RepoGraph, top_k: int = 6) -> list[Symbol]:
    q = _tokens(f"{fp.title} {fp.description} {fp.category}")
    scored: list[tuple[float, Symbol]] = []
    for s in repo.symbols:
        st = _symbol_tokens(s)
        if not st:
            continue
        overlap = len(q & st)
        score = float(overlap)
        if any(t in s.name.lower() for t in q):
            score += 0.5
        if s.kind == "class":
            score += 0.3
        # entry-point heuristic: cli/api features rarely share keywords with
        # a generic ``main`` body, so give such symbols a relevance floor.
        if s.name.lower() in _ENTRY_NAMES and fp.category in _ENTRY_CATEGORIES:
            score += 1.5
        if score <= 0:
            continue
        scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [s for _, s in scored[:top_k]]

    # Fallback: if we found little, always include likely entry points so the
    # LLM at least sees the wiring code before declaring a feature missing.
    if len(picked) < top_k:
        have = {(s.file, s.start_line) for s in picked}
        for s in repo.symbols:
            if s.name.lower() in _ENTRY_NAMES and (s.file, s.start_line) not in have:
                picked.append(s)
                have.add((s.file, s.start_line))
                if len(picked) >= top_k:
                    break
    return picked


def _snippet(repo: RepoGraph, s: Symbol, max_lines: int = 40) -> str:
    full = Path(repo.root) / s.file
    try:
        lines = full.read_text("utf-8", "replace").splitlines()
    except Exception:
        return ""
    start = max(0, s.start_line - 1)
    end = min(len(lines), s.end_line)
    body = lines[start : min(end, start + max_lines)]
    return "\n".join(body)


SYSTEM = """You are a meticulous code auditor for Vibe Guard.
You are given ONE feature point that a project is supposed to implement, plus a set of
candidate code symbols WITH their source snippets and exact file:line locations.

Decide whether the feature is implemented by the provided code. Be skeptical: a function
named like the feature is NOT proof — the body must actually do it. Cite evidence ONLY from
the provided snippets using their real file and line numbers. Never invent file paths or lines.

status meaning:
- "implemented": the feature is clearly and fully realised by the cited code.
- "partial": some of it exists but it is incomplete, stubbed, or only partially wired.
- "missing": no provided code implements it.
- "unclear": cannot tell from the provided snippets.

Respond with STRICT JSON only.
"""

USER_TMPL = """FEATURE POINT {fid}: {title}
Description: {desc}
Category: {category} | Priority: {priority}

=== CANDIDATE SYMBOLS (with source) ===
{candidates}

Return JSON:
{{
  "status": "implemented|partial|missing|unclear",
  "confidence": 0.0-1.0,
  "evidence": [
    {{"file": "<path from candidates>", "lines": "<start-end>", "symbol": "<name>", "note": "<why this is evidence>"}}
  ],
  "reasoning": "<2-4 sentence justification grounded in the snippets>"
}}
If status is "missing", evidence may be an empty list.
"""


def _format_candidates(repo: RepoGraph, syms: list[Symbol]) -> str:
    blocks: list[str] = []
    for s in syms:
        loc = f"{s.file}:{s.start_line}-{s.end_line}"
        head = f"{s.kind} {s.name}" + (f" (in {s.parent})" if s.parent else "")
        snip = _snippet(repo, s)
        blocks.append(f"--- {head} @ {loc} ---\n{snip}")
    return "\n\n".join(blocks) if blocks else "(no candidate symbols matched)"


def align_feature(fp: FeaturePoint, repo: RepoGraph, llm: LLMClient) -> AlignmentResult:
    cands = _retrieve(fp, repo)
    if not cands:
        return AlignmentResult(
            feature_id=fp.id,
            feature_title=fp.title,
            status=AlignStatus.MISSING,
            confidence=0.6,
            evidence=[],
            reasoning="No symbols in the repository matched this feature's keywords.",
        )

    prompt = USER_TMPL.format(
        fid=fp.id,
        title=fp.title,
        desc=fp.description,
        category=fp.category,
        priority=fp.priority,
        candidates=_format_candidates(repo, cands)[:14000],
    )
    data = llm.chat_json(SYSTEM, prompt, max_tokens=1500)

    try:
        status = AlignStatus(str(data.get("status", "unclear")).lower())
    except ValueError:
        status = AlignStatus.UNCLEAR

    evidence: list[Evidence] = []
    for ev in data.get("evidence", []) or []:
        evidence.append(
            Evidence(
                file=str(ev.get("file", "")),
                lines=str(ev.get("lines", "")),
                symbol=str(ev.get("symbol", "")),
                note=str(ev.get("note", ""))[:300],
            )
        )

    conf = data.get("confidence", 0.0)
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.0

    return AlignmentResult(
        feature_id=fp.id,
        feature_title=fp.title,
        status=status,
        confidence=conf,
        evidence=evidence,
        reasoning=str(data.get("reasoning", ""))[:600],
    )


def align(repo: RepoGraph, feature_points: list[FeaturePoint], llm: LLMClient,
          progress=None) -> list[AlignmentResult]:
    results: list[AlignmentResult] = []
    for fp in feature_points:
        results.append(align_feature(fp, repo, llm))
        if progress:
            progress(fp)
    return results
