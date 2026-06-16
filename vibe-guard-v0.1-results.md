# Vibe Guard v0.1 — Implementation & Evaluation Results

_Date: 2026-06-16 · MVP stage 1 of `ARCHITECTURE.md` · LLM backend: DeepSeek
(`deepseek-chat`, OpenAI-compatible endpoint)_

---

## 1. MVP implementation summary

Vibe Guard v0.1 is a working CLI that takes a Python repository and emits a
single Markdown **verification report** combining a *functional alignment*
checklist (does the code do what the README/requirements claim?) with
*security & dependency red lines*.

### Pipeline (all five stages implemented)

| Stage | Module | What it does |
|---|---|---|
| 1. Ingest | `ingest.py` | Walks the repo, builds a **tree-sitter** symbol graph for Python (functions / classes / methods with signatures, docstrings and call references), and collects the README + dependency manifests. |
| 2. Normalize | `normalizer.py` | LLM turns the README + optional user requirements into a **`RequirementSpec`** — a list of discrete, testable feature points. |
| 3. Route A — feature alignment | `align.py` | Per feature point: retrieve candidate symbols (keyword overlap + entry-point heuristics), pull their **real source snippets**, and have the LLM judge `implemented / partial / missing / unclear` with **`file:line` evidence grounded in the snippets**. |
| 4. Route C — security & deps | `security.py` | Runs **Semgrep** (registry rule packs), **Gitleaks** (secrets), **Trivy** (CVEs, optional) and an in-house **hallucinated-dependency** detector (PyPI existence check for declared/imported packages). |
| 5. Aggregate / report | `report.py`, `cli.py` | Rolls everything into a Markdown report with a verdict (`PASS` / `PASS WITH WARNINGS` / `NEEDS REVIEW`), a feature checklist table, and severity-grouped security findings. |

### Directory structure

```
vibe-guard-mvp/
├── README.md
├── pyproject.toml                 # installable: `vibe-guard` entry point
├── vibe-guard-v0.1-results.md     # this file
├── vibe_guard/                    # ~1,580 LOC
│   ├── __init__.py
│   ├── __main__.py                # python -m vibe_guard
│   ├── cli.py                     # typer + rich CLI (`scan`, `version`)
│   ├── models.py                  # pydantic v2 models (graph, spec, findings, report)
│   ├── llm.py                     # OpenAI-compatible client (DeepSeek) + JSON parsing
│   ├── ingest.py                  # tree-sitter symbol graph
│   ├── normalizer.py              # README/requirements → RequirementSpec
│   ├── align.py                   # Route A: retrieval + LLM feature alignment
│   ├── security.py                # Route C: semgrep/gitleaks/trivy/hallucinated-dep
│   └── report.py                  # Markdown report renderer
├── test_projects/                 # the three evaluated repos (vendored)
│   ├── python-slugify/            # small
│   ├── itsdangerous/              # medium
│   └── click/                     # large
├── reports_slugify.md             # full per-project reports
├── reports_itsdangerous.md
└── reports_click.md
```

### Tech stack

- **Parsing:** `tree-sitter` 0.25 + `tree-sitter-python` 0.25
- **Models / CLI:** `pydantic` v2, `typer`, `rich`
- **LLM:** `openai` SDK pointed at DeepSeek (`base_url=https://api.deepseek.com/v1`, `model=deepseek-chat`)
- **Security tooling:** Semgrep 1.166 (rule packs `p/python`, `p/security-audit`, `p/secrets`), Gitleaks 8.18.4, Trivy (optional — see §4), in-house dependency detector.

### The hallucinated-dependency detector (in-house, Route C)

This is the piece most specific to *AI-generated* code. It:

1. Parses **real** dependency declarations only — `requirements*.txt`,
   `[project].dependencies` / `optional-dependencies` / `build-system.requires`
   in `pyproject.toml` (via `tomllib`), `install_requires`/`extras_require` in
   `setup.py`, and `setup.cfg`. (The v0.1 *first cut* naively grepped every
   quoted string and produced false positives — see §5; this was fixed.)
2. Extracts top-level third-party imports (excluding stdlib and local packages),
   mapping common import→distribution aliases (`yaml`→`pyyaml`, `cv2`→`opencv-python`, …).
3. Queries the **PyPI JSON API** for each:
   - declared package that 404s → **CRITICAL** (`VG-DEP-001`, hallucinated/slopsquatted manifest entry);
   - imported module whose distribution 404s → **CRITICAL** (`VG-DEP-002`);
   - imported-but-undeclared third-party module → **MEDIUM** (`VG-DEP-003`), demoted to **INFO** when only used in test/docs/example code.

---

## 2. Evaluation set

Three real open-source Python projects were fetched (via the GitHub
codeload tarball endpoint) and bucketed by non-test LOC:

| Bucket | Project | Non-test LOC | Py files | Symbols | README |
|---|---|---|---|---|---|
| **Small** (<500) | [`un33k/python-slugify`](https://github.com/un33k/python-slugify) | 444 | 7 | 99 | ✅ |
| **Medium** (500–5000) | [`pallets/itsdangerous`](https://github.com/pallets/itsdangerous) | 1,231 | 15 | 144 | ✅ |
| **Large** (>5000) | [`pallets/click`](https://github.com/pallets/click) | 12,493 | 63 | 1,838 | ✅ |

Each was scanned with `vibe-guard scan <repo> --no-trivy -o report.md`.
All three runs completed in **under 75 seconds** wall-clock and **13–14 LLM
calls** each (one normalize call + one per feature point).

---

## 3. Per-project verification results

### 3.1 Small — `python-slugify` &nbsp; Verdict: ✅ PASS

> _"A Python library and CLI tool to generate URL-friendly slugs from Unicode
> strings, with extensive configuration options."_

- **Functional alignment:** **13 / 13 feature points implemented** (0 partial,
  0 missing). Every README feature — Unicode transliteration, `allow_unicode`,
  HTML-entity conversion, max-length truncation, custom separators, stopwords,
  custom regex/replacements, case control, `save_order`, and the CLI incl. the
  `--` multi-value separator — was matched to concrete `file:line` evidence in
  `slugify/slugify.py` and the test suite.
- **Security / deps:** 0 critical, 0 high, **2 medium**:
  - Semgrep: `insecure-hash-algorithm-sha1` (SHA-1 used for a non-security slug hash — a true positive pattern-wise, low real risk).
  - `VG-DEP-003`: `unidecode` imported but only declared as an *extra* (`extras_require`), so flagged as not-pinned-in-base.

> This is the ideal happy-path: a small, faithful library passes cleanly.

### 3.2 Medium — `itsdangerous` &nbsp; Verdict: ✅ PASS

> _"A library for cryptographically signing data to pass it safely through
> untrusted environments, with support for serialization, compression,
> timestamps, and URL-safe encoding."_

- **Functional alignment:** **11 / 12 implemented**, 1 "missing".
  - The one **missing** flag — *"Salt support"* — is a **false negative**:
    `itsdangerous` does support a `salt` argument on `Signer`, but the keyword
    retriever surfaced the wrong symbols and the LLM (correctly, given what it
    was shown) reported no evidence. See §5.
- **Security / deps:** 0 critical, 0 high, **2 medium** (+3 INFO dev-only
  imports such as `pytest`, `freezegun`):
  - Semgrep: `exec-detected` — `exec()` use (in the typed-signature shim / test
    helpers); a real audit-grade signal.

### 3.3 Large — `click` &nbsp; Verdict: 🟡 PASS WITH WARNINGS

> _"A Python package for creating command line interfaces with composable
> decorators, automatic help pages, and arbitrary command nesting."_

- **Ingest scaled cleanly:** 63 files / 26,704 LOC / **1,838 symbols** parsed in
  a couple of seconds.
- **Functional alignment:** 9 implemented, **1 partial**, **2 missing**:
  - *"Lazy loading of subcommands"* and *"Prompt for missing options"* were
    flagged **missing**, and *"Callback execution order"* **partial** — all
    three are **retrieval false negatives**. Click implements every one of them;
    at 1,838 symbols, single-pass keyword retrieval (top-6 candidates per
    feature) did not surface the right code. This is the headline limitation of
    v0.1 (§5) and the clearest signal that retrieval must scale better.
- **Security / deps:** **0 findings** above INFO. Semgrep produced no results on
  this mature codebase; the only dependency notes were INFO-level dev/test
  imports (`pytest`, `pallets_sphinx_themes`, `typing_extensions`).

### Cross-project summary

| Project | Features ✅/🟡/❌/❓ | Sec 🟥/🟧/🟨 | Verdict | LLM calls | Wall-clock |
|---|---|---|---|---|---|
| python-slugify | 13 / 0 / 0 / 0 | 0 / 0 / 2 | ✅ PASS | 14 | ~72 s |
| itsdangerous | 11 / 0 / 1 / 0 | 0 / 0 / 2 | ✅ PASS | 13 | ~51 s |
| click | 9 / 1 / 2 / 0 | 0 / 0 / 0 | 🟡 WARN | 13 | ~58 s |

**Validation note (synthetic fixture):** a deliberately broken fixture
(`_smoke/`) with a hardcoded AWS key, a hallucinated package (`leftpadinator`),
a non-existent manifest entry (`totally-not-a-real-pkg-xyz123`) and a missing
CLI was scanned to confirm true-positive behaviour: Gitleaks flagged the secret
(HIGH), the dependency detector flagged both fake packages (CRITICAL), and Route
A correctly marked the absent feature `missing`.

---

## 4. Problems encountered & how they were handled

| # | Problem | Resolution |
|---|---|---|
| 1 | **GitHub token invalid** (`401 Bad credentials`) for both REST API and git push. | Test projects were fetched via the public `codeload.github.com` tarball endpoint instead. The final push could **not** be authenticated — see §7. |
| 2 | **`git clone` over the smart HTTP protocol timed out**; only tarball downloads worked. | Switched all repo acquisition to `codeload` tarballs. |
| 3 | **Trivy binary download kept truncating** (8.5 MB / segfault) and its vuln-DB needs a large network pull. | Trivy is wired in and used when present, but the evaluation runs used `--no-trivy`. The in-house dependency detector covers the supply-chain dimension in the meantime. |
| 4 | **Semgrep `--config auto` refuses to run with metrics disabled.** | Switched to explicit registry rule packs (`p/python`, `p/security-audit`, `p/secrets`), which work offline-of-metrics. |
| 5 | **Hallucinated-dep detector had bad false positives** — its first cut grepped every quoted string in `setup.py` / `pyproject.toml`, "discovering" packages like `console-scripts`, `utf-8`, `r`, and an `import … as` artifact `as`. | Rewrote manifest parsing to read **only** real dependency fields (`tomllib` for `pyproject.toml`; scoped `install_requires`/`extras_require` blocks for `setup.py`), and fixed the import parser to handle `import a, b as c`. Re-scan dropped slugify from 8 bogus CRITICALs to 0. |
| 6 | **`pip install` blocked by PEP 668** (externally-managed env). | Created a project virtualenv. |

---

## 5. Findings, limitations & improvement ideas

**What works well**
- **Ingest** scales to ~1,800 symbols / 27k LOC in seconds and is stable.
- **Normalizer** produces clean, atomic, well-categorised feature points (12–13
  per project) that read like a real acceptance checklist.
- **Route A evidence is grounded**: because the LLM only sees real, located
  snippets, its `file:line` citations were accurate in every spot-check — no
  hallucinated file paths.
- The **dependency detector** (post-fix) is precise and catches the
  AI-specific slopsquatting failure mode that off-the-shelf SAST misses.

**Limitations (honest)**
1. **Retrieval is the bottleneck at scale.** Per-feature top-6 keyword retrieval
   missed real implementations in the large repo (click: lazy subcommands,
   option prompting) and even in the medium one (itsdangerous: `salt`). Reported
   "missing" on a mature library is therefore **more likely a retrieval miss
   than a real gap** today. *Fix:* embeddings-based retrieval, call-graph
   expansion around seed symbols, and a second "are you sure it's missing?"
   verification pass before declaring `missing`.
2. **Single-language.** Python only; the symbol graph and import analysis need
   per-language tree-sitter grammars to generalise.
3. **Trivy/CVE coverage was not exercised** in this run (binary + DB fetch
   issues), so runtime-dependency CVEs are currently a blind spot.
4. **Tests counted as evidence.** Route A sometimes cites test files as proof a
   feature exists. That's often legitimate, but it should be labelled
   ("verified by tests" vs "implemented in source") rather than conflated.
5. **No confidence calibration / no cross-checking** between Route A and the
   symbol graph — a `missing` verdict isn't yet reconciled against "but a
   same-named public symbol exists."
6. **Cost/concurrency:** alignment is sequential (one LLM call per feature).
   Fan-out would cut wall-clock substantially.

---

## 6. Next steps (toward v0.2)

1. **Better retrieval for Route A** — embeddings + call-graph neighbourhood
   expansion; add a *verification pass* that re-checks every `missing`/`partial`
   verdict against the full symbol of the same name before finalising. This
   directly attacks the click/itsdangerous false negatives.
2. **Finish Route C** — bundle a working Trivy (or `pip-audit`/OSV) path for
   real CVE coverage; add license red lines.
3. **Multi-language ingest** — JS/TS and Go grammars; generalise import/dep
   analysis.
4. **Evidence typing** — distinguish "implemented in source" from "covered by
   tests"; attach the exact lines that satisfy each feature.
5. **Parallelise alignment** and add response caching to cut latency/cost.
6. **CI mode** — non-zero exit on red lines, JSON/SARIF output, and a PR comment
   formatter so Vibe Guard can gate AI-generated PRs.
7. **Calibration harness** — a labelled benchmark of (repo, feature, truth) so
   precision/recall of Route A can be tracked across versions.

---

## 7. Delivery / push status

All artifacts are written under `~/cc/vibe-guard-mvp/` (MVP code in
`vibe_guard/`, evaluated repos in `test_projects/`, per-project reports
`reports_*.md`, and this summary).

The provided `GITHUB_TOKEN` returned **`401 Bad credentials`** for both the REST
API (`/user`, `/rate_limit`) and authenticated git push, so the repository could
**not** be pushed from this run. To publish, re-run with a valid token:

```bash
cd ~/cc/vibe-guard-mvp
git init && git add -A && git commit -m "Vibe Guard MVP v0.1 + evaluation"
git remote add origin https://<TOKEN>@github.com/Rossery/vibe-guard.git
git push -u origin main          # or use the Contents API with a valid token
```

A ready-to-push git repository (with commit) has been prepared locally so that
only valid credentials are needed to complete delivery.
