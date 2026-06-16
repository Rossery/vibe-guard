"""Tests for edit distance and typo-squat detection."""

from __future__ import annotations

from vibe_guard.hallucheck.fuzzy import find_typosquat, levenshtein
from vibe_guard.hallucheck.models import Ecosystem


def test_levenshtein_basics():
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "abd") == 1          # substitution
    assert levenshtein("requests", "requets") == 1  # deletion (transposition shows as 2? no -> 1 del + ...)
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3


def test_typosquat_pypi_classic():
    # one deletion from "requests"
    assert find_typosquat("requets", Ecosystem.PYPI) == "requests"
    # extra char vs beautifulsoup4
    assert find_typosquat("beautfulsoup4", Ecosystem.PYPI) == "beautifulsoup4"


def test_typosquat_does_not_flag_legit_popular():
    # exact popular names must never be flagged
    assert find_typosquat("requests", Ecosystem.PYPI) is None
    assert find_typosquat("numpy", Ecosystem.PYPI) is None


def test_typosquat_distant_name_not_flagged():
    # a totally unrelated internal package
    assert find_typosquat("my-internal-svc-utils", Ecosystem.PYPI) is None


def test_typosquat_short_names_need_tight_distance():
    # "flask" -> "flaskk" is distance 1 → flagged
    assert find_typosquat("flaskk", Ecosystem.PYPI) == "flask"
    # a short name needing 2 edits to reach any popular name is NOT flagged
    # ("six" is the nearest popular name and sits at distance 2)
    assert find_typosquat("nax", Ecosystem.PYPI) is None


def test_typosquat_npm():
    assert find_typosquat("expres", Ecosystem.NPM) == "express"
    assert find_typosquat("lodahs", Ecosystem.NPM) == "lodash"
    assert find_typosquat("react", Ecosystem.NPM) is None
