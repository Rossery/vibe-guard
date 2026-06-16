"""Tests for manifest + import parsing across PyPI and npm."""

from __future__ import annotations

from pathlib import Path

from vibe_guard.hallucheck.models import Ecosystem
from vibe_guard.hallucheck.parsers import (
    collect_js_imports,
    collect_python_imports,
    parse_npm_manifests,
    parse_package_json,
    parse_python_manifests,
    normalize_pypi,
)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_normalize_pypi():
    assert normalize_pypi("Flask") == "flask"
    assert normalize_pypi("ruamel.yaml") == "ruamel-yaml"
    assert normalize_pypi("typing_extensions") == "typing-extensions"


def test_parse_requirements_txt(tmp_path: Path):
    _write(tmp_path, "requirements.txt",
           "requests==2.31.0\n# a comment\nFlask>=2.0\n-e .\ngit+https://x/y\n\nrich\n")
    deps = {d.name for d in parse_python_manifests(str(tmp_path))}
    assert "requests" in deps
    assert "flask" in deps
    assert "rich" in deps
    # editable + vcs lines are skipped
    assert not any(d.startswith("git") for d in deps)


def test_parse_pyproject(tmp_path: Path):
    _write(tmp_path, "pyproject.toml", """
[build-system]
requires = ["setuptools>=61"]

[project]
name = "demo"
dependencies = ["pydantic>=2.5", "typer"]

[project.optional-dependencies]
dev = ["pytest>=8"]
""")
    deps = {d.name for d in parse_python_manifests(str(tmp_path))}
    assert {"pydantic", "typer", "pytest", "setuptools"} <= deps


def test_parse_setup_py(tmp_path: Path):
    _write(tmp_path, "setup.py", """
from setuptools import setup
setup(name="x", install_requires=["click>=8", 'requests'])
""")
    deps = {d.name for d in parse_python_manifests(str(tmp_path))}
    assert {"click", "requests"} <= deps


def test_collect_python_imports_excludes_stdlib_and_local(tmp_path: Path):
    _write(tmp_path, "mypkg/__init__.py", "")
    _write(tmp_path, "mypkg/core.py",
           "import os\nimport sys\nimport requests\nfrom yaml import safe_load\n"
           "import mypkg.helpers\nfrom . import sibling\n")
    _write(tmp_path, "mypkg/helpers.py", "x = 1\n")
    deps = {d.name: d for d in collect_python_imports(str(tmp_path))}
    assert "requests" in deps
    # yaml import maps to the pyyaml distribution via the alias table
    assert "pyyaml" in deps
    # stdlib + local + relative imports excluded
    assert "os" not in deps and "sys" not in deps
    assert "mypkg" not in deps and "sibling" not in deps
    assert deps["requests"].declared is False
    assert deps["pyyaml"].import_name == "yaml"


def test_parse_package_json():
    content = """
    {
      "name": "demo",
      "dependencies": {"react": "^18.0.0", "@scope/util": "1.0.0"},
      "devDependencies": {"jest": "^29"}
    }
    """
    deps = {d.name for d in parse_package_json(content)}
    assert deps == {"react", "@scope/util", "jest"}


def test_parse_npm_manifests_skips_node_modules(tmp_path: Path):
    _write(tmp_path, "package.json", '{"dependencies": {"express": "^4"}}')
    _write(tmp_path, "node_modules/foo/package.json", '{"dependencies": {"evil": "1"}}')
    deps = {d.name for d in parse_npm_manifests(str(tmp_path))}
    assert "express" in deps
    assert "evil" not in deps  # node_modules pruned


def test_collect_js_imports(tmp_path: Path):
    _write(tmp_path, "src/app.ts",
           "import React from 'react';\n"
           "import {x} from '@scope/util';\n"
           "const fs = require('fs');\n"
           "const local = require('./local');\n"
           "import axios from 'axios';\n"
           "const lazy = await import('lodash');\n")
    deps = {d.name for d in collect_js_imports(str(tmp_path))}
    assert {"react", "@scope/util", "axios", "lodash"} <= deps
    # node builtins and relative imports excluded
    assert "fs" not in deps
    assert not any(d.startswith(".") for d in deps)


def test_scoped_npm_subpath_collapses_to_package(tmp_path: Path):
    _write(tmp_path, "a.js", "import x from '@babel/core/lib/index';\nimport y from 'lodash/get';\n")
    deps = {d.name for d in collect_js_imports(str(tmp_path))}
    assert "@babel/core" in deps
    assert "lodash" in deps
