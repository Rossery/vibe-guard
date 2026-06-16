"""Parsers that turn a project tree into a list of :class:`Dependency`.

Two complementary views are extracted per ecosystem:

* **declared** dependencies — from manifests (``requirements.txt``,
  ``pyproject.toml``, ``setup.py``, ``setup.cfg``, ``Pipfile``,
  ``package.json``).
* **imported** dependencies — third-party ``import`` / ``require`` statements
  found in source, mapped from import name to distribution name.

Comparing the two surfaces *imported-but-not-declared* (supply-chain hygiene)
and *declared-but-nonexistent* (hallucination) problems.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

from .models import Dependency, Ecosystem

# ---- directory pruning ---------------------------------------------------- #
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".tox", "build", "dist", ".eggs",
    "site-packages", ".idea", ".vscode", ".ruff_cache", "coverage",
}

# ---- python import-name -> PyPI distribution aliases ---------------------- #
PYPI_IMPORT_ALIASES: dict[str, str] = {
    "yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "skimage": "scikit-image", "dotenv": "python-dotenv",
    "jwt": "pyjwt", "dateutil": "python-dateutil", "git": "gitpython",
    "serial": "pyserial", "OpenSSL": "pyopenssl", "Crypto": "pycryptodome",
    "attr": "attrs", "google": "google-api-python-client", "yaml_": "pyyaml",
    "psycopg2": "psycopg2-binary", "Image": "pillow", "win32api": "pywin32",
}

# ---- node builtin modules (never registry-checked) ------------------------ #
NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "console", "crypto",
    "dgram", "dns", "events", "fs", "http", "http2", "https", "net", "os",
    "path", "perf_hooks", "process", "querystring", "readline", "stream",
    "string_decoder", "timers", "tls", "tty", "url", "util", "v8", "vm",
    "worker_threads", "zlib", "module", "async_hooks", "inspector",
})


# --------------------------------------------------------------------------- #
# generic helpers
# --------------------------------------------------------------------------- #
def normalize_pypi(name: str) -> str:
    """PEP 503 normalisation: lower-case, runs of ``[-_.]`` collapse to ``-``."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _pypi_dep_name(spec: str) -> str | None:
    """Extract the distribution name from a PEP 508 requirement spec."""
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return normalize_pypi(m.group(1)) if m else None


def _walk_files(root: str, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(suffixes):
                out.append(Path(dirpath) / fn)
    return out


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_bytes()[:limit].decode("utf-8", "replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# PyPI: declared manifests
# --------------------------------------------------------------------------- #
def parse_python_manifests(root: str) -> list[Dependency]:
    """Parse every Python manifest under *root* into declared dependencies."""
    deps: list[Dependency] = []
    seen: set[str] = set()
    root_path = Path(root)

    def add(name: str | None, src: Path, raw: str) -> None:
        if not name:
            return
        rel = str(src.relative_to(root_path))
        k = f"{name}@{rel}"
        if k in seen:
            return
        seen.add(k)
        deps.append(Dependency(name=name, ecosystem=Ecosystem.PYPI,
                               source_file=rel, declared=True, raw=raw))

    for path in _walk_files(root, (".txt", ".toml", ".cfg", ".py", "Pipfile")):
        base = path.name.lower()
        if base.endswith(".txt") and ("requirement" in base or "constraint" in base):
            for line in _read(path).splitlines():
                line = line.split("#")[0].strip()
                if not line or line.startswith(("-", "git+", "http", "//", ".")):
                    continue
                add(_pypi_dep_name(line), path, line)
        elif base == "pyproject.toml":
            _parse_pyproject(_read(path), path, add)
        elif base == "setup.py":
            _parse_setup_py(_read(path), path, add)
        elif base == "setup.cfg":
            _parse_setup_cfg(_read(path), path, add)
        elif base == "pipfile":
            _parse_pipfile(_read(path), path, add)
    return deps


def _parse_pyproject(content: str, path: Path, add) -> None:
    try:
        data = tomllib.loads(content)
    except (tomllib.TOMLDecodeError, ValueError):
        return
    proj = data.get("project", {}) or {}
    for d in proj.get("dependencies", []) or []:
        add(_pypi_dep_name(d), path, d)
    for grp in (proj.get("optional-dependencies", {}) or {}).values():
        for d in grp or []:
            add(_pypi_dep_name(d), path, d)
    for d in (data.get("build-system", {}) or {}).get("requires", []) or []:
        add(_pypi_dep_name(d), path, d)
    poetry = ((data.get("tool", {}) or {}).get("poetry", {}) or {})
    for name in (poetry.get("dependencies", {}) or {}):
        if name.lower() != "python":
            add(normalize_pypi(name), path, name)


def _parse_setup_py(content: str, path: Path, add) -> None:
    for block in re.findall(
        r"(?:install_requires|setup_requires|tests_require|extras_require)\s*=\s*([\[{].*?[\]}])",
        content, re.DOTALL,
    ):
        for s in re.findall(r"[\"']([^\"']+)[\"']", block):
            add(_pypi_dep_name(s), path, s)


def _parse_setup_cfg(content: str, path: Path, add) -> None:
    m = re.search(r"install_requires\s*=\s*\n((?:\s+.+\n)+)", content)
    if m:
        for line in m.group(1).splitlines():
            add(_pypi_dep_name(line.strip()), path, line.strip())


def _parse_pipfile(content: str, path: Path, add) -> None:
    # Pipfile is TOML; [packages] / [dev-packages] tables.
    try:
        data = tomllib.loads(content)
    except (tomllib.TOMLDecodeError, ValueError):
        return
    for table in ("packages", "dev-packages"):
        for name in (data.get(table, {}) or {}):
            add(normalize_pypi(name), path, name)


# --------------------------------------------------------------------------- #
# PyPI: imports
# --------------------------------------------------------------------------- #
def _local_python_modules(root: str) -> set[str]:
    names: set[str] = set()
    root_path = Path(root)
    for path in _walk_files(root, (".py",)):
        rel = path.relative_to(root_path)
        if rel.parts:
            names.add(rel.parts[0].removesuffix(".py"))
        names.add(path.stem)
        if path.name == "__init__.py" and len(rel.parts) >= 2:
            names.add(rel.parts[-2])
    return {n for n in names if n}


_IMPORT_FROM = re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\b")
_IMPORT_PLAIN = re.compile(r"^\s*import\s+(.+)")


def collect_python_imports(root: str) -> list[Dependency]:
    """Collect third-party top-level imports (excluding stdlib & local)."""
    stdlib = set(sys.stdlib_module_names)
    local = _local_python_modules(root)
    root_path = Path(root)
    found: dict[str, str] = {}  # import name -> first file

    def add(mod: str, file: str) -> None:
        mod = mod.strip().split(".")[0].strip()
        if (mod and mod.isidentifier() and not mod.startswith("_")
                and mod not in stdlib and mod not in local and mod not in found):
            found[mod] = file

    for path in _walk_files(root, (".py",)):
        rel = str(path.relative_to(root_path))
        for raw in _read(path).splitlines():
            mf = _IMPORT_FROM.match(raw)
            if mf:
                if not mf.group(1).startswith("."):
                    add(mf.group(1), rel)
                continue
            mp = _IMPORT_PLAIN.match(raw)
            if mp and " = " not in raw:
                for seg in mp.group(1).split(","):
                    name = seg.strip().split(" as ")[0].strip()
                    add(name, rel)

    deps: list[Dependency] = []
    for mod, file in sorted(found.items()):
        dist = PYPI_IMPORT_ALIASES.get(mod, mod)
        deps.append(Dependency(name=normalize_pypi(dist), ecosystem=Ecosystem.PYPI,
                               source_file=file, declared=False,
                               raw=f"import {mod}", import_name=mod))
    return deps


# --------------------------------------------------------------------------- #
# npm: declared manifests
# --------------------------------------------------------------------------- #
def parse_package_json(content: str, source_file: str = "package.json") -> list[Dependency]:
    """Parse a ``package.json`` into declared npm dependencies."""
    import json
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return []
    deps: list[Dependency] = []
    for section in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
        block = data.get(section) or {}
        if not isinstance(block, dict):
            continue
        for name, ver in block.items():
            deps.append(Dependency(name=name.strip().lower(), ecosystem=Ecosystem.NPM,
                                   source_file=source_file, declared=True,
                                   raw=f"{name}@{ver}"))
    return deps


def parse_npm_manifests(root: str) -> list[Dependency]:
    """Parse all ``package.json`` files under *root* (skipping node_modules)."""
    deps: list[Dependency] = []
    root_path = Path(root)
    for path in _walk_files(root, ("package.json",)):
        rel = str(path.relative_to(root_path))
        deps.extend(parse_package_json(_read(path), rel))
    return deps


# --------------------------------------------------------------------------- #
# npm: imports / requires
# --------------------------------------------------------------------------- #
_JS_IMPORT = re.compile(r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_DYNAMIC = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")


def _npm_package_root(spec: str) -> str | None:
    """``@scope/pkg/sub`` -> ``@scope/pkg``; ``pkg/sub`` -> ``pkg``.

    Relative (``./x``), absolute and builtin specifiers return ``None``.
    """
    spec = spec.strip()
    if not spec or spec.startswith((".", "/")):
        return None
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    head = spec.split("/")[0]
    if head in NODE_BUILTINS or spec.startswith("node:"):
        return None
    return head


def collect_js_imports(root: str) -> list[Dependency]:
    """Collect npm package specifiers from JS/TS ``import``/``require`` sites."""
    root_path = Path(root)
    found: dict[str, str] = {}
    for path in _walk_files(root, (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        if path.name.endswith(".d.ts"):
            continue
        rel = str(path.relative_to(root_path))
        text = _read(path)
        for rx in (_JS_IMPORT, _JS_REQUIRE, _JS_DYNAMIC):
            for spec in rx.findall(text):
                pkg = _npm_package_root(spec)
                if pkg and pkg not in found:
                    found[pkg] = rel
    return [
        Dependency(name=pkg, ecosystem=Ecosystem.NPM, source_file=file,
                   declared=False, raw=f"import '{pkg}'", import_name=pkg)
        for pkg, file in sorted(found.items())
    ]
