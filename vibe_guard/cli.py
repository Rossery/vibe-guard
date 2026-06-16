"""Vibe Guard CLI (MVP v0.1).

    vibe-guard scan <repo> [--requirements ...] [-o report.md]

Runs the full pipeline: ingest -> normalize -> align (Route A) ->
security scan (Route C) -> Markdown report.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .align import align
from .ingest import ingest
from .llm import LLMClient
from .models import ScanReport
from .normalizer import normalize
from .report import render_markdown
from .security import scan_security

app = typer.Typer(add_completion=False, help="Vibe Guard — verify AI-generated code.")
console = Console()


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"vibe-guard {__version__}")


@app.command()
def scan(
    repo: str = typer.Argument(..., help="Path to the repository to scan."),
    requirements: str = typer.Option(
        "", "--requirements", "-r",
        help="Extra requirements text or path to a requirements/spec file."),
    output: str = typer.Option("", "--output", "-o", help="Write Markdown report here."),
    api_key: str = typer.Option("", "--api-key", help="LLM API key (else env)."),
    base_url: str = typer.Option("", "--base-url", help="LLM base URL."),
    model: str = typer.Option("", "--model", help="LLM model name."),
    no_pypi: bool = typer.Option(False, "--no-pypi", help="Skip PyPI existence checks."),
    no_trivy: bool = typer.Option(False, "--no-trivy", help="Skip Trivy."),
    no_align: bool = typer.Option(False, "--no-align", help="Skip Route A (no LLM)."),
) -> None:
    """Scan a repository and produce a verification report."""
    root = Path(repo).resolve()
    if not root.exists():
        console.print(f"[red]Path not found:[/red] {repo}")
        raise typer.Exit(2)

    # extra requirements: file or literal
    req_text = requirements
    if requirements:
        p = Path(requirements)
        if p.exists() and p.is_file():
            req_text = p.read_text("utf-8", "replace")

    console.rule(f"[bold]Vibe Guard v{__version__}[/bold]  ·  {root.name}")

    # 1) ingest
    console.print("[cyan]›[/cyan] Ingesting repository (tree-sitter)…")
    repo_graph = ingest(str(root))
    console.print(
        f"  files={len(repo_graph.files)} loc={repo_graph.total_loc} "
        f"symbols={len(repo_graph.symbols)} "
        f"readme={'yes' if repo_graph.readme else 'no'} "
        f"deps={list(repo_graph.dependency_files.keys())}"
    )

    llm = None
    spec = None
    alignment = []

    # 2) normalize + 3) align (need LLM)
    if not no_align:
        try:
            llm = LLMClient(
                api_key=api_key or None,
                base_url=base_url or None,
                model=model or None,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]LLM unavailable ({e}); skipping Route A.[/yellow]")
            no_align = True

    if not no_align and llm is not None:
        console.print("[cyan]›[/cyan] Normalizing requirements → feature points (LLM)…")
        spec = normalize(repo_graph, llm, user_requirements=req_text)
        console.print(f"  derived {len(spec.feature_points)} feature points "
                      f"(source={spec.source})")

        console.print("[cyan]›[/cyan] Route A: aligning code to features (LLM)…")
        done = {"n": 0}

        def _prog(fp):
            done["n"] += 1
            console.print(f"    [{done['n']}/{len(spec.feature_points)}] {fp.id} {fp.title[:50]}")

        alignment = align(repo_graph, spec.feature_points, llm, progress=_prog)

    if spec is None:
        # still produce an (empty) spec so the report renders
        from .models import RequirementSpec
        spec = RequirementSpec(project_name=root.name,
                               summary="(Route A skipped)", source="none")

    # 4) security
    console.print("[cyan]›[/cyan] Route C: security & dependency scan…")
    tool_runs, findings = scan_security(
        repo_graph, check_pypi=not no_pypi, use_trivy=not no_trivy)
    for tr in tool_runs:
        status = "ok" if tr.ok else ("n/a" if not tr.available else f"err:{tr.error[:40]}")
        console.print(f"    {tr.tool:<18} {status:<12} findings={tr.findings_count}")

    # 5) aggregate + report
    report = ScanReport(
        project_name=spec.project_name or root.name,
        root=str(root),
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        repo=repo_graph,
        spec=spec,
        alignment=alignment,
        security=findings,
        tool_runs=tool_runs,
    )

    _print_summary(report)

    md = render_markdown(report)
    if output:
        Path(output).write_text(md, "utf-8")
        console.print(f"[green]✓ Report written:[/green] {output}")
    else:
        console.print(md)

    if llm is not None:
        console.print(f"[dim]LLM calls={llm.calls} "
                      f"tokens={llm.prompt_tokens}+{llm.completion_tokens}[/dim]")


def _print_summary(report: ScanReport) -> None:
    ac = report.align_counts
    sc = report.security_counts
    t = Table(title="Verification summary", show_header=True)
    t.add_column("Dimension")
    t.add_column("Result")
    if report.alignment:
        t.add_row("Features ✅/🟡/❌/❓",
                  f"{ac.get('implemented',0)}/{ac.get('partial',0)}/"
                  f"{ac.get('missing',0)}/{ac.get('unclear',0)}")
    t.add_row("Security crit/high/med/low",
              f"{sc.get('critical',0)}/{sc.get('high',0)}/"
              f"{sc.get('medium',0)}/{sc.get('low',0)}")
    console.print(t)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(app())  # type: ignore[func-returns-value]
