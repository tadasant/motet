"""Schema and migrations for Motet.

Postgres holds the data *and* the job queue (``SKIP LOCKED``). There is no Redis and no
vector store — see the tripwires in AGENTS.md before adding either.
"""

from .migrate import MIGRATIONS_DIR, Migration, MigrationError, discover, migrate

__all__ = ["MIGRATIONS_DIR", "Migration", "MigrationError", "discover", "migrate"]
