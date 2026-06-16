"""Aggregate pipeline outputs into a Markdown verification report."""

from __future__ import annotations

from .models import AlignStatus, ScanReport, Severity

_STATUS_ICON = {
    AlignStatus.IMPLEMENTED: "✅",
    AlignStatus.PARTIAL: "🟡",
    AlignStatus.MISSING: "❌",
    AlignStatus.UNCLEAR: "❓",
}

_SEV_ICON = {
    Severity.CRITICAL: "🟥 CRITICAL",
    Severity.HIGH: "🟧 HIGH",
    Severity.MEDIUM: "🟨 MEDIUM",
    Severity.LOW: "🟦 LOW",
    Severity.INFO: "⬜ INFO",
}

_SEV_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]


def _evidence_str(ev_list) -> str:
    if not ev_list:
        return "—"
    parts = []
    for e in ev_list[:4]:
        loc = e.file + (f":{e.lines}" if e.lines else "")
        sym = f" `{e.symbol}`" if e.symbol else ""
        parts.append(f"`{loc}`{sym}")
    return "<br>".join(parts)


def render_markdown(report: ScanReport) -> str:
    L: list[str] = []
    ac = report.align_counts
    sc = report.security_counts
    total_feat = len(report.alignment)

    L.append(f"# Vibe Guard Verification Report — {report.project_name}")
    L.append("")
    L.append(f"_Generated: {report.generated_at}_  ·  Vibe Guard v0.1")
    L.append("")

    # ---- summary ----
    L.append("## 1. Summary")
    L.append("")
    L.append(f"> {report.spec.summary or '(no summary)'}")
    L.append("")
    L.append(f"- **Files (Python):** {len(report.repo.files)}  ·  **LOC:** {report.repo.total_loc}  ·  **Symbols:** {len(report.repo.symbols)}")
    L.append(f"- **Feature points:** {total_feat}")
    if total_feat:
        L.append(
            f"  - ✅ implemented: {ac.get('implemented',0)}  ·  "
            f"🟡 partial: {ac.get('partial',0)}  ·  "
            f"❌ missing: {ac.get('missing',0)}  ·  "
            f"❓ unclear: {ac.get('unclear',0)}"
        )
    crit = sc.get("critical", 0) + sc.get("high", 0)
    L.append(f"- **Security findings:** {len(report.security)} "
             f"({sc.get('critical',0)} critical, {sc.get('high',0)} high, "
             f"{sc.get('medium',0)} medium, {sc.get('low',0)} low)")
    L.append("")

    # ---- red lines / verdict ----
    verdict, reasons = _verdict(report)
    L.append(f"### Verdict: {verdict}")
    for r in reasons:
        L.append(f"- {r}")
    L.append("")

    # ---- Route A table ----
    L.append("## 2. Functional alignment (Route A)")
    L.append("")
    if not report.alignment:
        L.append("_No feature points were derived (missing README / requirements)._")
    else:
        L.append("| Feature | Status | Conf. | Evidence (file:line) | Notes |")
        L.append("|---|---|---|---|---|")
        for a in report.alignment:
            icon = _STATUS_ICON.get(a.status, "❓")
            note = (a.reasoning or "").replace("\n", " ").replace("|", "/")[:160]
            L.append(
                f"| **{a.feature_id}** {a.feature_title.replace('|','/')[:60]} "
                f"| {icon} {a.status.value} | {a.confidence:.2f} "
                f"| {_evidence_str(a.evidence)} | {note} |"
            )
    L.append("")

    # ---- Route C ----
    L.append("## 3. Security & dependency red lines (Route C)")
    L.append("")
    L.append("**Scanner runs:**")
    L.append("")
    L.append("| Tool | Available | OK | Findings | Time | Note |")
    L.append("|---|---|---|---|---|---|")
    for tr in report.tool_runs:
        L.append(
            f"| {tr.tool} | {'yes' if tr.available else 'no'} | "
            f"{'yes' if tr.ok else 'no'} | {tr.findings_count} | "
            f"{tr.duration_s:.1f}s | {(tr.error or '')[:60]} |"
        )
    L.append("")

    if report.security:
        by_sev = {s: [] for s in _SEV_ORDER}
        for f in report.security:
            by_sev[f.severity].append(f)
        for sev in _SEV_ORDER:
            items = by_sev[sev]
            if not items:
                continue
            L.append(f"### {_SEV_ICON[sev]} ({len(items)})")
            L.append("")
            L.append("| Tool | Rule | Finding | Location |")
            L.append("|---|---|---|---|")
            for f in items[:40]:
                loc = (f.file + (f":{f.line}" if f.line else "")) if f.file else "—"
                L.append(
                    f"| {f.tool} | `{f.rule_id}` | "
                    f"{f.title.replace('|','/')[:120]} | `{loc}` |"
                )
            if len(items) > 40:
                L.append(f"| … | | _{len(items)-40} more_ | |")
            L.append("")
    else:
        L.append("_No security or dependency findings._")
        L.append("")

    # ---- feature point catalog ----
    L.append("## 4. Requirement spec (normalized feature points)")
    L.append("")
    if report.spec.feature_points:
        for fp in report.spec.feature_points:
            L.append(f"- **{fp.id}** ({fp.category}/{fp.priority}) — "
                     f"**{fp.title}**: {fp.description}")
    else:
        L.append("_(none)_")
    L.append("")

    return "\n".join(L)


def _verdict(report: ScanReport) -> tuple[str, list[str]]:
    sc = report.security_counts
    ac = report.align_counts
    reasons: list[str] = []
    blocking = False

    if sc.get("critical", 0):
        blocking = True
        reasons.append(f"🟥 {sc['critical']} CRITICAL security/dependency finding(s) — "
                       "includes possible hallucinated packages.")
    if sc.get("high", 0):
        reasons.append(f"🟧 {sc['high']} HIGH finding(s) (secrets / risky patterns).")
    missing = ac.get("missing", 0)
    partial = ac.get("partial", 0)
    if missing:
        reasons.append(f"❌ {missing} required feature point(s) appear unimplemented.")
        if missing >= max(1, len(report.alignment) // 3):
            blocking = True
    if partial:
        reasons.append(f"🟡 {partial} feature point(s) only partially implemented.")
    if not reasons:
        reasons.append("No blocking issues detected by the configured checks.")

    verdict = "❌ NEEDS REVIEW" if blocking else (
        "🟡 PASS WITH WARNINGS" if len(reasons) > 1 or reasons[0].startswith(("🟧", "🟡"))
        else "✅ PASS")
    return verdict, reasons
