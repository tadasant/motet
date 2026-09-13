"""Runtime settings, and the one switch that decides whether they exist at all.

The ``settings`` table (migration 0012) holds per-stage LLM model and effort overrides the
admin screen writes (motet#92). Every setting before it was an environment variable owned
by the private infrastructure repo's service definitions, validated at startup — and "an
unknown slug is a startup crash" is the property a row that changes what the next job runs
would quietly give up. So the table is **a laptop and staging affordance, and production
never honours it**:

* :data:`SETTINGS_WRITABLE_ENV` unset — every deployed production service — means the API
  refuses to write a row *and* the worker refuses to read one. Refusing both, not only the
  write, is what makes "production is env-only" a property rather than a habit: a row that
  reached the table some other way (a migration, a restored dump, a flag switched off
  after a row was written) is inert, and the boot log still describes what the worker
  runs.
* Set to a true value — a laptop, staging — the API writes rows through its validation and
  the worker installs them per job.

**Why this lives in ``motet_db``**, which knows nothing of LLMs: the switch is about this
table, both deployables must read it identically, and ``motet_db`` is the one package both
import. :mod:`motet_db.allowlist` sits here for the same reason. What a key *means* is
``motet_inference.llm.config``'s business, and what a value may be is validated there.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

import psycopg

logger = logging.getLogger("motet.db.settings")

#: ``1`` on a laptop and in staging; unset in production, where the table is inert.
SETTINGS_WRITABLE_ENV: Final = "MOTET_SETTINGS_WRITABLE"

_TRUE: Final = frozenset({"1", "true", "yes"})
_FALSE: Final = frozenset({"", "0", "false", "no"})

#: Unparseable values already warned about. This is read per job and per health request,
#: so without it one typo in a service definition would be a warning a second, forever.
_warned: set[str] = set()


def settings_writable(env: Mapping[str, str]) -> bool:
    """Whether this deployment honours ``settings`` rows at all.

    **Fails closed.** Anything that is not recognisably true — a typo, ``on``, ``enabled``
    — reads as off and says so, because the off side costs an operator a dropdown that will
    not save, while the on side would put runtime-mutable config somewhere nobody chose it.
    """
    raw = env.get(SETTINGS_WRITABLE_ENV, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw not in _FALSE and raw not in _warned:
        _warned.add(raw)
        logger.warning(
            "%s=%r is not a boolean; treating it as off, so settings rows are ignored",
            SETTINGS_WRITABLE_ENV,
            raw,
        )
    return False


def load(conn: psycopg.Connection[Any], prefix: str) -> dict[str, str]:
    """Every row whose key starts with ``prefix``, as a plain mapping."""
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE starts_with(key, %s) ORDER BY key", (prefix,)
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def put(conn: psycopg.Connection[Any], key: str, value: str | None) -> None:
    """Set one row, or delete it when ``value`` is ``None``. The caller has validated it."""
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = %s", (key,))
        return
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, value),
    )


def count(conn: psycopg.Connection[Any], prefix: str) -> int:
    """How many rows under ``prefix`` exist — what ``/internal/health`` asks."""
    row = conn.execute(
        "SELECT count(*) AS n FROM settings WHERE starts_with(key, %s)", (prefix,)
    ).fetchone()
    return int(row["n"]) if row is not None else 0
