"""`motet-enrich` holds no database and no vault, and that is asserted rather than intended.

Design option D2 (motet#102) puts the agentic run in a container whose service account has
no project roles at all, because the code it shells out to is third-party npm driving a
browser over pages nobody at Motet wrote — and **any process in a Cloud Run container can
mint that container's service-account token from the metadata server**. The boundary is
therefore only as good as the claim that this package cannot reach a database or a key, so
that claim is a test.

The shape is ``voice/tests/test_no_database_access.py``'s, for invariant 2, one service
along: the dependency list is read out of the package's own ``pyproject.toml``, and the
source tree is read for imports. ``bin/build-images`` makes the same claim against the
built image, which is the half this file cannot make.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE / "src" / "motet_enrich"

#: Packages this service must never depend on, and what each one would mean.
FORBIDDEN = {
    "motet-db": "the schema and every query — this service has no database",
    "motet-vault": "the decrypt half; only the worker may open a credential (invariant 8)",
    "motet-workers": "the pipeline, which would drag the database in behind it",
    "motet-api": "the same, and a dependency cycle",
    "psycopg": "a driver for a database this container has no credential for",
    "google-cloud-kms": "the key the worker holds and this service must not",
}

FORBIDDEN_MODULES = {"motet_db", "motet_vault", "motet_workers", "motet_api", "psycopg"}


def _dependencies() -> list[str]:
    with (PACKAGE / "pyproject.toml").open("rb") as handle:
        return list(tomllib.load(handle)["project"]["dependencies"])


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_the_package_does_not_depend_on(package: str) -> None:
    for declared in _dependencies():
        name = declared.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip()
        assert name != package, f"motet-enrich must not depend on {package}: {FORBIDDEN[package]}"


def test_no_module_imports_the_database_or_the_vault() -> None:
    """Every import in the package, including the lazy ones inside functions.

    Walked with ``ast`` rather than grepped, so that a comment mentioning ``motet_db``
    cannot fail this and an import hidden inside a ``try`` cannot pass it.
    """
    offenders: list[str] = []
    for module in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_MODULES:
                    offenders.append(f"{module.relative_to(PACKAGE)}: {name}")
    assert not offenders, f"motet-enrich imports what it must not reach: {offenders}"
