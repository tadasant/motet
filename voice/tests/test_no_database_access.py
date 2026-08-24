"""Invariant 2, as a test rather than as a comment.

**The voice service never touches the news DB.** That is an architectural boundary, and the
failure mode it guards against is not a bug that shows up in a diff review — it is somebody
adding one convenient import six months from now and nothing going red.

Two checks, because there are two ways to cross the line: declaring the dependency, and
importing it anyway.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "motet_voice"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: Names that mean "this process can read the corpus". ``psycopg`` is here as well as
#: ``motet_db`` because reaching for the driver directly is the obvious way around a rule
#: that only names the wrapper.
FORBIDDEN_MODULES = ("motet_db", "psycopg", "sqlalchemy", "asyncpg")


def test_voice_declares_no_database_dependency() -> None:
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dependencies = [name.lower() for name in manifest["project"]["dependencies"]]
    for forbidden in ("motet-db", "psycopg", "sqlalchemy", "asyncpg"):
        assert not any(dependency.startswith(forbidden) for dependency in dependencies), (
            f"voice/pyproject.toml declares {forbidden}. The voice service takes a session "
            "config and calls tools; it has no database credential and no schema knowledge."
        )


def test_no_module_imports_the_database() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno} imports {name}")
    assert not offenders, (
        "the voice service must not reach the news database (invariant 2):\n" + "\n".join(offenders)
    )
