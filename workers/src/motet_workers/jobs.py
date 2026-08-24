"""The job queue: claim, finish, fail, retry — all of it in Postgres.

Postgres is the queue as well as the datastore. Reaching for Redis here is a named
tripwire in AGENTS.md, and at this scale it would buy nothing: ``SELECT ... FOR UPDATE
SKIP LOCKED`` is a correct work queue, and having the queue in the same transaction as the
data means a job can be enqueued by the same commit that creates the row it refers to.
That property is worth more than throughput this system will never need — without it,
there is always a window where a source item exists and nothing will ever process it.

**Serialization keys are how invariant 6 is enforced.** A job may carry a
``serialize_key``; a worker holding one runs alone for that key. Ingestion sets it to the
user id, because dedup compares a new source item against the current window and two
concurrent runs would race into duplicate news items. The mechanism is a Postgres
advisory lock rather than a status column, because a lock is released when the connection
dies and a status column is not — a worker killed mid-job would otherwise block that
user's ingestion until someone noticed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .queues import Queue

logger = logging.getLogger("motet.worker.jobs")

#: Attempts before a job stops being retried and becomes a visible failure. Small on
#: purpose: every stage here is either deterministic or vendor-backed, and a vendor
#: outage that outlasts five tries needs a human rather than a tighter loop.
DEFAULT_MAX_ATTEMPTS = 5

#: Retry backoff, in seconds, indexed by attempt number. Past the end of the list the
#: last value repeats.
BACKOFF_SECONDS: tuple[int, ...] = (5, 30, 120, 600)

#: How long a job whose serialization key is busy waits before trying again. Short: the
#: holder is another ingestion run for the same user, which finishes in seconds.
BUSY_RETRY_SECONDS = 5


@dataclass(frozen=True)
class Job:
    id: int
    queue: Queue
    payload: Mapping[str, Any]
    attempts: int
    serialize_key: str | None


def enqueue(
    conn: psycopg.Connection[Any],
    queue: Queue,
    payload: Mapping[str, Any],
    *,
    serialize_key: str | None = None,
    delay_seconds: int = 0,
) -> int:
    """Add a job. Call inside the transaction that creates the work it refers to."""
    row = conn.execute(
        """
        INSERT INTO jobs (queue, payload, serialize_key, run_at)
        VALUES (%s, %s::jsonb, %s, now() + make_interval(secs => %s))
        RETURNING id
        """,
        (queue.value, json.dumps(dict(payload)), serialize_key, delay_seconds),
    ).fetchone()
    assert row is not None
    job_id = row["id"] if isinstance(row, dict) else row[0]
    assert isinstance(job_id, int)
    return job_id


def claim(conn: psycopg.Connection[Any], queue: Queue) -> Job | None:
    """Take the oldest ready job on ``queue``, or return None.

    ``FOR UPDATE SKIP LOCKED`` inside the subquery is what makes this safe to run from
    several workers at once: a row another transaction already holds is skipped rather
    than waited on, so N workers claim N different jobs instead of queueing behind one.

    Commit before running the job. The row is marked ``running`` so a crash leaves
    evidence, and ``attempts`` is incremented on claim rather than on failure so a job
    that kills its worker outright still counts toward the retry ceiling — otherwise a
    poison job retries forever.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE jobs
            SET state = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM jobs
                WHERE queue = %s AND state = 'ready' AND run_at <= now()
                ORDER BY run_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, queue, payload, attempts, serialize_key
            """,
            (queue.value,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Job(
        id=row["id"],
        queue=Queue(row["queue"]),
        payload=row["payload"],
        attempts=row["attempts"],
        serialize_key=row["serialize_key"],
    )


def complete(conn: psycopg.Connection[Any], job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET state = 'done', last_error = NULL, updated_at = now() WHERE id = %s",
        (job_id,),
    )


def fail(
    conn: psycopg.Connection[Any],
    job: Job,
    error: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Record a failure. Returns True if the job will be retried.

    Each stage is retried independently — they have different rate limits and failure
    modes, which is why they are separate queues on one table in the first place. A
    Cartesia 429 must not stall dedup, and a dedup retry must not re-synthesize audio.
    """
    if job.attempts >= max_attempts:
        conn.execute(
            "UPDATE jobs SET state = 'failed', last_error = %s, updated_at = now() WHERE id = %s",
            (error[:2000], job.id),
        )
        logger.error("job %d on %s failed permanently: %s", job.id, job.queue.value, error)
        return False

    delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
    conn.execute(
        """
        UPDATE jobs
        SET state = 'ready', last_error = %s, locked_at = NULL,
            run_at = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = %s
        """,
        (error[:2000], delay, job.id),
    )
    logger.warning(
        "job %d on %s failed (attempt %d), retrying in %ds: %s",
        job.id,
        job.queue.value,
        job.attempts,
        delay,
        error,
    )
    return True


def defer(conn: psycopg.Connection[Any], job: Job, *, seconds: int = BUSY_RETRY_SECONDS) -> None:
    """Put a job back without counting it as an attempt.

    Used when a serialization key is busy: nothing went wrong, this worker simply is not
    the one that gets to run it. Charging an attempt for that would let a busy user's
    ingestion exhaust its retries without a single failure.
    """
    conn.execute(
        """
        UPDATE jobs
        SET state = 'ready', locked_at = NULL, attempts = attempts - 1,
            run_at = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = %s
        """,
        (seconds, job.id),
    )


def lock_key(serialize_key: str) -> int:
    """Map a serialization key onto the bigint an advisory lock is taken on.

    Hashed here rather than with Postgres's ``hashtext``, which is an internal function
    with no compatibility guarantee across major versions. A hash whose value changed
    under a database upgrade would silently stop serializing anything.
    """
    digest = hashlib.sha256(serialize_key.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def try_lock(conn: psycopg.Connection[Any], serialize_key: str) -> bool:
    """Take the session-level advisory lock for ``serialize_key``, without waiting."""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s) AS locked", (lock_key(serialize_key),)
    ).fetchone()
    assert row is not None
    locked = row["locked"] if isinstance(row, dict) else row[0]
    return bool(locked)


def unlock(conn: psycopg.Connection[Any], serialize_key: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key(serialize_key),))


def queue_depths(conn: psycopg.Connection[Any]) -> dict[str, dict[str, int]]:
    """Ready/running/failed counts per queue — what a health check reports."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT queue, state, count(*) AS n FROM jobs GROUP BY queue, state")
        rows = cur.fetchall()
    depths: dict[str, dict[str, int]] = {}
    for row in rows:
        depths.setdefault(row["queue"], {})[row["state"]] = row["n"]
    return depths
