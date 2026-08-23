"""Forward-only SQL migration runner.

Migrations are plain ``.sql`` files in ``db/migrations``, named ``NNNN_description.sql``
and applied in filename order. Each runs inside a transaction together with the insert
that records it, so a failure leaves nothing half-applied.

**Forward-only.** Never edit a migration that has been applied anywhere — write a new one.
The runner does not track checksums, so an edited file simply never re-runs, and the
schema silently diverges between environments.

No ORM and no migration framework on purpose: the schema is small, and a plain runner is
one file a reader can hold in their head. See AGENTS.md.
"""

from __future__ import annotations

import argparse
import logging
import os
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
    """Apply every pending migration. Returns the versions applied, in order."""
    migrations = discover(directory)
    newly_applied: list[str] = []

    with psycopg.connect(database_url) as conn:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending Motet migrations.")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection URL (default: $DATABASE_URL)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.database_url:
        parser.error("no database URL: pass --database-url or set DATABASE_URL")

    applied = migrate(args.database_url)
    logger.info("migrations up to date (%d applied this run)", len(applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
