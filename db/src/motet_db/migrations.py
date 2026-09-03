"""Forward-only SQL migration runner. Imported, never executed.

Migrations are plain ``.sql`` files in ``db/migrations``, named ``NNNN_description.sql``
and applied in filename order. Each runs inside a transaction together with the insert
that records it, so a failure leaves nothing half-applied.

**Forward-only.** Never edit a migration that has been applied anywhere — write a new one.
The runner does not track checksums, so an edited file simply never re-runs, and the
schema silently diverges between environments.

The one carve-out is a ``--`` comment, and it is a carve-out precisely because it cannot
have that consequence: a comment never reaches the database, so an applied migration and
an edited copy of it produce identical schemas and nothing can diverge. What the rule is
protecting is the SQL. Anything that changes what a migration *does* — including adding or
removing a statement — is not covered, however small it looks. Correcting a comment that
has gone stale is worth doing in place: the misleading sentence is at the line a reader
meets first, and a correction filed only in a later migration does not reach them.

No ORM and no migration framework on purpose: the schema is small, and a plain runner is
one file a reader can hold in their head. See AGENTS.md.

**This module is the runner; :mod:`motet_db.migrate` next door is the CLI**, and the split
is structural rather than cosmetic. ``motet_db/__init__.py`` re-exports :func:`migrate`,
and ``python -m motet_db.migrate`` imports the package *before* executing the named module
— so while the runner lived in the module ``python -m`` targets, ``runpy`` executed that
file a second time under a second name, with a second copy of every module-level object.
That is motet#27, and it is the same defect ``motet_workers.runner`` shipped as motet#21.
Nothing in this package may import ``migrate.py``; ``db/tests/test_db_entrypoints.py``
fails if anything does. (The directory name collision is only apparent: ``db/migrations/``
holds the ``.sql`` files this module reads, and is not a Python package.)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

#: The advisory lock one migration run holds against another on the same database.
#:
#: Two runs applying a *pending* migration at the same moment both execute the same
#: ``CREATE TABLE`` — one wins and the other fails with "relation already exists", which
#: is a confusing way to be told something entirely ordinary happened. It is reachable
#: whenever two things migrate one database at once: two ``bin/ci`` runs on a machine
#: sharing the default ``DATABASE_URL``, or a deploy job retried while the first attempt
#: is still going. An advisory lock rather than a table, because it is released when the
#: connection dies — a process killed mid-migration must not leave the next one blocked.
#:
#: An arbitrary constant, and it only has to be distinct from other advisory locks taken
#: against the same database. The queue's keys (``motet_workers.jobs.lock_key``) are
#: SHA-256 derived, so a collision would take a deliberate search.
_MIGRATION_LOCK_KEY = 0x6D6F7465_74646201  # "mote" "tdb" 01


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text()


class MigrationError(RuntimeError):
    pass


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return every migration in the directory, in application order."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration {path.name!r} does not match NNNN_lower_snake_case.sql"
            )
        migrations.append(Migration(version=match.group(1), path=path))

    versions = [m.version for m in migrations]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise MigrationError(f"duplicate migration version(s): {sorted(duplicates)}")
    return migrations


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_CREATE_TRACKING_TABLE)
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the versions applied, in order.

    One run at a time per database: whoever gets the advisory lock applies, and anyone
    else waits and then finds there is nothing left to do. Without it, two runs racing on
    a fresh database both execute the first pending migration and one of them fails on a
    table the other had just created.
    """
    migrations = discover(directory)
    newly_applied: list[str] = []

    with psycopg.connect(database_url) as conn:
        # Session-level, so it is held across the per-migration commits below and released
        # when this connection closes — including when the process is killed.
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.commit()

        already = applied_versions(conn)
        conn.commit()

        for migration in migrations:
            if migration.version in already:
                continue
            logger.info("applying migration %s (%s)", migration.version, migration.path.name)
            with conn.cursor() as cur:
                cur.execute(migration.sql)  # type: ignore[arg-type,unused-ignore]
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (migration.version,)
                )
            conn.commit()
            newly_applied.append(migration.version)

    return newly_applied
