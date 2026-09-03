"""Migration entry point. Executed, never imported.

    python -m motet_db.migrate            # apply everything pending to $DATABASE_URL
    bin/migrate --database-url=...        # the same thing, from a checkout

The runner itself lives in :mod:`motet_db.migrations`, and this file holds the CLI and
nothing else. **Nothing in this package may import this module.** ``python -m
motet_db.migrate`` imports the package ``motet_db`` first and only then executes
``migrate.py`` as ``__main__`` — so a module ``__init__`` has already pulled into
``sys.modules`` on the way past is executed a *second* time under a second name, and
``runpy`` says so:

    RuntimeWarning: 'motet_db.migrate' found in sys.modules after import of package
    'motet_db', but prior to execution of 'motet_db.migrate'; this may result in
    unpredictable behaviour

That warning was printed by every ``bin/ci`` run and every ``bin/migrate``, because
``__init__`` re-exported :func:`~motet_db.migrations.migrate` from here. Two copies of one
module do not share module-level state; nothing here was stateful enough for that to have
hurt, but a client, a cache, a connection pool, or a class used with ``isinstance``
declared at the top of this file would not have survived being built twice. That is
motet#27, and it is the same defect :mod:`motet_workers.runner` shipped as motet#21 — so
the rule is structural rather than a thing to remember: the runner is importable and lives
next door, this file is the executable, and ``db/tests/test_db_entrypoints.py`` fails if the
two ever merge back.
"""

from __future__ import annotations

import argparse
import logging
import os

from .migrations import migrate

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    # `prog` is set because argparse would otherwise take it from `sys.argv[0]`, which
    # under `python -m` is the file — so `--help` announced itself as `migrate.py`, a name
    # that appears nowhere anyone could run it.
    parser = argparse.ArgumentParser(
        prog="motet-migrate", description="Apply pending Motet migrations."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection URL (default: $DATABASE_URL)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.database_url:
        parser.error("no database URL: pass --database-url or set DATABASE_URL")

    applied = migrate(args.database_url)
    logger.info("migrations up to date (%d applied this run)", len(applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
