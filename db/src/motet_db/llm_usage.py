"""The ``llm_usage`` ledger: one row per completion, appended by the worker, summed by the API.

Migration 0015, motet#92. The third record of what a completion cost, beside the
``motet.llm.tokens`` metric (the fleet, no ids) and the per-episode log line (ids, not
summable), and the only one that can be summed per user — which is the whole reason it
exists. Nothing here knows what a stage or a model *is*: ``motet_db`` sits below
``motet_inference``, so both are strings, and pricing is the caller's job because the
catalogue is where prices live.

**Two sources of truth for spend now exist, and they will disagree.** A row the worker
could not write is logged and dropped; a metric batch the exporter could not ship is lost
the other way. Neither is corrected against the other, and neither should be read as the
other's audit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import psycopg

#: How long a row is kept. A quarter: the screen reads a seven-day column and a total over
#: whatever is retained, and a quarter is long enough to compare this month with the last
#: two while bounding the table by throughput rather than by the deployment's age — the
#: argument ``motet_workers.jobs.FAILED_RETENTION_SECONDS`` makes, with the same number.
RETENTION_SECONDS: Final = 90 * 24 * 3600

#: Rows one retention statement deletes, and the most statements one sweep runs — the
#: bounds ``motet_workers.jobs.prune`` uses, for the same reason: a delete is its own
#: transaction on an autocommit connection, so the sweep never holds more than one batch
#: of row locks and can stop between any two.
PRUNE_BATCH_SIZE: Final = 1000
PRUNE_MAX_BATCHES: Final = 10

#: Parameters: the retention window in seconds, and the batch size.
PRUNE_SQL: Final = """
    DELETE FROM llm_usage
    WHERE id IN (
        SELECT id FROM llm_usage
        WHERE occurred_at < now() - make_interval(secs => %s::int)
        ORDER BY occurred_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
"""


@dataclass(frozen=True)
class UsageRow:
    """One completion, as the worker hands it over."""

    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_ttl: str | None


def insert(
    conn: psycopg.Connection[Any],
    rows: Sequence[UsageRow],
    *,
    subject: str | None,
    job_id: int | None,
) -> None:
    """Append one job's completions.

    ``user_id`` is resolved from ``subject`` here rather than by the caller, which is the
    worker loop: it knows a job's payload and nothing about the tables a payload points at.
    A ``si_`` subject is a source item and an ``ep_`` one an episode; anything else has no
    user this table can name, and the row is still written.
    """
    conn.cursor().executemany(
        """
        INSERT INTO llm_usage (
            stage, model, input_tokens, output_tokens, reasoning_tokens,
            cache_read_tokens, cache_write_tokens, cache_ttl, user_id, subject, job_id
        )
        VALUES (
            %(stage)s, %(model)s, %(input)s, %(output)s, %(reasoning)s,
            %(cache_read)s, %(cache_write)s, %(cache_ttl)s,
            COALESCE(
                (SELECT user_id FROM source_items WHERE id = %(subject)s),
                (SELECT user_id FROM episodes WHERE id = %(subject)s)
            ),
            %(subject)s, %(job_id)s
        )
        """,
        [
            {
                "stage": row.stage,
                "model": row.model,
                "input": row.input_tokens,
                "output": row.output_tokens,
                "reasoning": row.reasoning_tokens,
                "cache_read": row.cache_read_tokens,
                "cache_write": row.cache_write_tokens,
                "cache_ttl": row.cache_ttl,
                "subject": subject,
                "job_id": job_id,
            }
            for row in rows
        ],
    )


@dataclass(frozen=True)
class Totals:
    """Summed token counts for one ``(user, stage, model, cache_ttl)`` cell.

    Grouped that far down because a price is per model and a cache write's price is per
    TTL, so a cell is the largest thing that can be priced exactly. The caller prices each
    cell and folds the cells into whatever it is displaying.
    """

    user_id: str | None
    user_email: str | None
    stage: str
    model: str
    cache_ttl: str | None
    completions: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class Ledger:
    """The ledger summed twice — over everything retained, and over the recent window."""

    since: datetime | None
    window_days: int
    total: list[Totals]
    window: list[Totals]


_TOTALS_SQL: Final = """
    SELECT u.user_id, users.email AS user_email, u.stage, u.model, u.cache_ttl,
           count(*) AS completions,
           coalesce(sum(u.input_tokens), 0)::bigint AS input_tokens,
           coalesce(sum(u.output_tokens), 0)::bigint AS output_tokens,
           coalesce(sum(u.reasoning_tokens), 0)::bigint AS reasoning_tokens,
           coalesce(sum(u.cache_read_tokens), 0)::bigint AS cache_read_tokens,
           coalesce(sum(u.cache_write_tokens), 0)::bigint AS cache_write_tokens
    FROM llm_usage u
    LEFT JOIN users ON users.id = u.user_id
    WHERE %(days)s::int IS NULL OR u.occurred_at >= now() - make_interval(days => %(days)s::int)
    GROUP BY 1, 2, 3, 4, 5
    ORDER BY 3, 4, 1
"""


def totals(conn: psycopg.Connection[Any], *, window_days: int) -> Ledger:
    """Every priced cell, all-time and over the last ``window_days``.

    The join onto ``users`` is for a label and nothing else — the ledger's own ``user_id``
    is what is grouped on, so a user row that is gone costs the label, not the money.
    """
    first = conn.execute("SELECT min(occurred_at) AS since FROM llm_usage").fetchone()
    since = first["since"] if first is not None else None
    if since is None:
        return Ledger(since=None, window_days=window_days, total=[], window=[])
    total = _cells(conn.execute(_TOTALS_SQL, {"days": None}).fetchall())
    window = _cells(conn.execute(_TOTALS_SQL, {"days": window_days}).fetchall())
    return Ledger(since=since, window_days=window_days, total=total, window=window)


def _cells(rows: list[dict[str, Any]]) -> list[Totals]:
    return [
        Totals(
            user_id=row["user_id"],
            user_email=row["user_email"],
            stage=row["stage"],
            model=row["model"],
            cache_ttl=row["cache_ttl"],
            completions=row["completions"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class Pruned:
    deleted: int
    capped: bool


def prune(
    conn: psycopg.Connection[Any],
    *,
    retention_seconds: int = RETENTION_SECONDS,
    batch_size: int = PRUNE_BATCH_SIZE,
    max_batches: int = PRUNE_MAX_BATCHES,
) -> Pruned:
    """Delete rows past :data:`RETENTION_SECONDS`, oldest first, in bounded batches.

    Refuses a connection that is not autocommit, for ``motet_workers.jobs.prune``'s reason:
    inside one transaction the batches would hold every lock to the end, and every test
    about *which* rows go would still pass.
    """
    if not conn.autocommit:
        raise ValueError("llm_usage.prune() needs an autocommit connection; see jobs.prune")
    deleted = 0
    for _ in range(max_batches):
        cursor = conn.execute(PRUNE_SQL, (retention_seconds, batch_size))
        deleted += cursor.rowcount
        if cursor.rowcount < batch_size:
            return Pruned(deleted=deleted, capped=False)
    return Pruned(deleted=deleted, capped=True)
