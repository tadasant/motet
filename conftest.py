"""Fixtures shared by every test package.

Two things live here because they are cross-cutting:

* **A migrated, empty database**, for the tests that exercise the pipeline for real. There
  is no in-memory substitute worth having — the queue is ``SELECT ... FOR UPDATE SKIP
  LOCKED``, read state is a partial index, and a claim's span is a ``CHECK`` constraint.
  A fake database would verify none of it.
* **``MOTET_INFERENCE_MODE=fake``, pinned for the whole session.** Invariant 7: no test in
  this repo may make a real vendor call. Set here rather than trusted from the environment,
  so a developer who exported ``real`` in their shell cannot spend money by running pytest.

Without ``DATABASE_URL`` the database-backed tests **skip** rather than fail, so a quick
``uv run pytest`` works with no Postgres. CI always has one, so that path is always covered
there — but a green local run without Postgres has not exercised it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

#: Truncated before every test. Ordered for readability only — `CASCADE` handles the
#: dependencies.
#:
#: ``sources`` is deliberately **absent**: migration 0002 seeds ``src_paste``, and
#: truncating the table would take that row with it and break paste-in — Phase 1's only
#: ingestion route — in a way that looks like an application bug. Connected sources added
#: by a test are removed by :data:`_DELETE_NON_SEED_SOURCES` instead.
TABLES = (
    "jobs",
    "source_items",
    "news_items",
    "news_item_sources",
    "episodes",
    "episode_segments",
    "segment_claims",
    "feed_tokens",
    "source_credentials",
    "oauth_states",
    "highlights",
)

#: Sources a test connected, without disturbing the seeded paste source. Cascades to that
#: source's credentials and source items, which is what makes the ordering above harmless.
_DELETE_NON_SEED_SOURCES = "DELETE FROM sources WHERE id <> 'src_paste'"


@pytest.fixture(scope="session", autouse=True)
def _never_call_a_vendor() -> None:
    """Invariant 7, enforced rather than assumed."""
    os.environ["MOTET_INFERENCE_MODE"] = "fake"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; skipping the database-backed tests")
    return url


@pytest.fixture(scope="session")
def _migrated(database_url: str) -> str:
    from motet_db import migrate

    migrate(database_url)
    return database_url


@pytest.fixture
def db(_migrated: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection to an empty database, truncated before each test.

    Truncated rather than rolled back: the pipeline tests run a worker, which opens its
    *own* connection, so work done inside an uncommitted transaction on this one would be
    invisible to it.
    """
    from motet_db import repo

    with repo.connect(_migrated) as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        conn.execute(_DELETE_NON_SEED_SOURCES)
        conn.commit()
        yield conn


@pytest.fixture
def object_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A local object store rooted in the test's own temporary directory."""
    from motet_storage import LocalObjectStore

    monkeypatch.setenv("MOTET_STORAGE_BACKEND", "local")
    monkeypatch.setenv("MOTET_STORAGE_DIR", str(tmp_path / "objects"))
    return LocalObjectStore(root=tmp_path / "objects")
