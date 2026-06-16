"""Ingest stage: load a repository and build a Python symbol graph.

MVP scope: Python only. Uses tree-sitter to extract functions, classes,
methods (with signatures, docstrings and called names), plus collects the
README and dependency manifests for downstream stages.
"""

from __future__ import annotations

import os
from pathlib import Path

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from .models import FileInfo, RepoGraph, Symbol

_PY_LANGUAGE = Language(tsp.language())

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".tox", "build", "dist", ".eggs", "site-packages",
    ".idea", ".vscode", ".ruff_cache",
}

DEP_FILES = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
    "setup.cfg", "Pipfile", "poetry.lock", "environment.yml", "constraints.txt",
}

README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]


def _make_parser() -> Parser:
    return Parser(_PY_LANGUAGE)


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _docstring(body: Node | None, src: bytes) -> str:
    if body is None:
        return ""
    for child in body.children:
        if child.type == "expression_statement" and child.children:
            inner = child.children[0]
            if inner.type == "string":
                doc = _text(inner, src).strip().strip("rRbBuU")
                return doc.strip("'\"").strip()[:300]
        break
    return ""


def _collect_calls(node: Node, src: bytes, out: set[str]) -> None:
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None:
            if fn.type == "identifier":
                out.add(_text(fn, src))
            elif fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                if attr is not None:
                    out.add(_text(attr, src))
    for c in node.children:
        _collect_calls(c, src, out)


def _signature(node: Node, src: bytes) -> str:
    name = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")
    nm = _text(name, src) if name else "?"
    pm = _text(params, src) if params else "()"
    return f"{nm}{pm}".replace("\n", " ")


def _extract_symbols(root: Node, src: bytes, rel: str) -> list[Symbol]:
    symbols: list[Symbol] = []

    def visit(node: Node, parent_class: str | None) -> None:
        for child in node.children:
            if child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                calls: set[str] = set()
                if body is not None:
                    _collect_calls(body, src, calls)
                symbols.append(
                    Symbol(
                        name=_text(name_node, src) if name_node else "?",
                        kind="method" if parent_class else "function",
                        file=rel,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        parent=parent_class,
                        signature=_signature(child, src),
                        docstring=_docstring(body, src),
                        calls=sorted(calls)[:30],
                    )
                )
                # nested defs (closures) — descend but keep parent context
                if body is not None:
                    visit(body, parent_class)
            elif child.type == "class_definition":
                name_node = child.child_by_field_name("name")
                cname = _text(name_node, src) if name_node else "?"
                body = child.child_by_field_name("body")
                symbols.append(
                    Symbol(
                        name=cname,
                        kind="class",
                        file=rel,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        parent=parent_class,
                        signature=cname,
                        docstring=_docstring(body, src),
                    )
                )
                if body is not None:
                    visit(body, cname)
            else:
                visit(child, parent_class)

    visit(root, None)
    return symbols


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        data = path.read_bytes()[:limit]
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def ingest(repo_path: str) -> RepoGraph:
    root = Path(repo_path).resolve()
    if not root.exists():
        raise FileNotFoundError(repo_path)

    parser = _make_parser()
    files: list[FileInfo] = []
    symbols: list[Symbol] = []
    dep_files: dict[str, str] = {}
    readme = ""
    total_loc = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root))

            if fn in DEP_FILES:
                dep_files[rel] = _read_text(full, 50_000)
            if not readme and fn in README_NAMES:
                readme = _read_text(full, 60_000)

            if not fn.endswith(".py"):
                continue
            try:
                src = full.read_bytes()
            except Exception:
                continue
            if len(src) > 2_000_000:  # skip absurdly large generated files
                continue
            loc = src.count(b"\n") + 1
            total_loc += loc
            files.append(FileInfo(path=rel, language="python", loc=loc))
            try:
                tree = parser.parse(src)
                symbols.extend(_extract_symbols(tree.root_node, src, rel))
            except Exception:
                continue

    return RepoGraph(
        root=str(root),
        files=files,
        symbols=symbols,
        total_loc=total_loc,
        readme=readme,
        dependency_files=dep_files,
    )
