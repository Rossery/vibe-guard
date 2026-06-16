"""Typo-squat detection via edit distance against popular package names.

Slopsquatting and typo-squatting both rely on a name that is *almost* a famous
package (``requets`` vs ``requests``, ``beautfulsoup4`` vs ``beautifulsoup4``).
We bundle a curated list of high-value targets per ecosystem and flag any
dependency whose name is within a small edit distance of one of them — without
being that package.
"""

from __future__ import annotations

from typing import Optional

from .models import Ecosystem

# A curated set of frequently-impersonated packages. Not exhaustive — the goal
# is high-precision coverage of the names attackers actually squat on.
POPULAR_PYPI: frozenset[str] = frozenset({
    "requests", "urllib3", "numpy", "pandas", "scipy", "matplotlib", "flask",
    "django", "fastapi", "pydantic", "sqlalchemy", "boto3", "botocore", "click",
    "pytest", "setuptools", "pip", "wheel", "six", "certifi", "idna", "chardet",
    "pyyaml", "jinja2", "werkzeug", "beautifulsoup4", "soupsieve", "lxml",
    "pillow", "opencv-python", "scikit-learn", "scikit-image", "tensorflow",
    "torch", "keras", "transformers", "openai", "anthropic", "tiktoken",
    "aiohttp", "httpx", "starlette", "uvicorn", "gunicorn", "celery", "redis",
    "pymongo", "psycopg2", "psycopg2-binary", "mysqlclient", "cryptography",
    "pyjwt", "passlib", "bcrypt", "python-dateutil", "pytz", "tqdm", "rich",
    "typer", "colorama", "packaging", "attrs", "markupsafe", "google-api-python-client",
    "protobuf", "grpcio", "websockets", "selenium", "scrapy", "tornado",
    "python-dotenv", "pyserial", "pyopenssl", "pycryptodome", "gitpython",
})

POPULAR_NPM: frozenset[str] = frozenset({
    "react", "react-dom", "vue", "angular", "lodash", "underscore", "axios",
    "express", "next", "webpack", "babel", "typescript", "eslint", "prettier",
    "jest", "mocha", "chai", "chalk", "commander", "yargs", "dotenv", "moment",
    "dayjs", "uuid", "rxjs", "redux", "react-redux", "jquery", "bootstrap",
    "tailwindcss", "styled-components", "graphql", "apollo-client", "socket.io",
    "ws", "node-fetch", "cross-env", "nodemon", "ts-node", "rimraf", "glob",
    "fs-extra", "cheerio", "puppeteer", "playwright", "vite", "rollup", "esbuild",
    "mongoose", "sequelize", "pg", "mysql2", "redis", "ioredis", "winston",
    "morgan", "cors", "body-parser", "jsonwebtoken", "bcrypt", "bcryptjs",
    "passport", "nanoid", "classnames", "zod", "immer", "formik", "yup",
})


def _popular_for(ecosystem: Ecosystem) -> frozenset[str]:
    return POPULAR_PYPI if ecosystem is Ecosystem.PYPI else POPULAR_NPM


def levenshtein(a: str, b: str) -> int:
    """Classic iterative Levenshtein (insert/delete/substitute) edit distance.

    O(len(a) * len(b)) time, O(min) space.
    """
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            insert = previous[j] + 1
            delete = current[j - 1] + 1
            substitute = previous[j - 1] + (ca != cb)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def _allowed_distance(name: str, max_distance: int) -> int:
    """Short names get a tighter bound to avoid false positives.

    A 4-char name within distance 2 matches half the dictionary; require
    distance 1 for names shorter than 6 chars.
    """
    if len(name) < 6:
        return min(1, max_distance)
    return max_distance


def find_typosquat(
    name: str,
    ecosystem: Ecosystem,
    max_distance: int = 2,
    popular: Optional[frozenset[str]] = None,
) -> Optional[str]:
    """Return the popular package *name* is squatting on, or ``None``.

    A name is *not* flagged if it is itself a popular package (an exact hit is a
    legitimate dependency, distance 0).
    """
    candidate = name.strip().lower()
    pop = popular if popular is not None else _popular_for(ecosystem)
    if not candidate or candidate in pop:
        return None
    bound = _allowed_distance(candidate, max_distance)
    best: Optional[str] = None
    best_dist = bound + 1
    for target in pop:
        # cheap length pre-filter
        if abs(len(target) - len(candidate)) > bound:
            continue
        d = levenshtein(candidate, target)
        if 0 < d < best_dist:
            best, best_dist = target, d
            if d == 1:
                break
    return best
