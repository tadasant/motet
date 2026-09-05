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
from enum import Enum
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

#: How long a ``running`` job may go **untouched** before another worker may take it.
#:
#: Without this a worker killed mid-job — a Cloud Run task timeout, an OOM, a revision
#: replacement, a SIGKILL — leaves its row in ``running`` forever. Nothing would ever
#: claim it again, the episode would sit in ``rendering``, and the only recovery would be
#: hand-written SQL against production, which invariant 10 forbids outright.
#:
#: **"Untouched" is now the load-bearing word, and it used to be "claimed" (motet#53).**
#: This was set to be longer than the slowest stage can legitimately take — but "the
#: slowest stage" is a guess about work whose size is the user's backlog, and a constant
#: cannot be longer than something unbounded. A script job took 2580s against a full
#: backlog, a second worker reclaimed it while the first was still working it, and the
#: whole stage — a 22k-token script completion, the entire grounding cascade, a complete
#: Cartesia synthesis — ran and billed twice for one episode. That is exactly the mistake
#: this comment already named as the more expensive of the two.
#:
#: So the constant no longer has to bound the work: a live worker pushes its own lease out
#: while it runs (:func:`touch`), and this bounds how long a job may go with **nobody
#: saying they are still on it**. Do not raise it to accommodate a slow stage — that is
#: the shape that failed. Lowering it is the interesting direction and is left alone here,
#: because a heartbeat that cannot reach Postgres has to be able to miss several in a row
#: without its job being taken.
STALE_LEASE_SECONDS = 1800

#: How often a running job's lease is pushed out while its handler is still working.
#:
#: Well under :data:`STALE_LEASE_SECONDS`, because the point is to survive missed touches:
#: at a minute apart, thirty in a row have to fail before a live worker loses its job. The
#: cost is one single-row ``UPDATE`` per minute per running job, against a job that is at
#: that moment calling a model.
LEASE_TOUCH_SECONDS = 60

#: The most wall-clock a single job may hold its lease open by touching it.
#:
#: **This is the other failure direction, and it is the one that is unrecoverable.** A
#: heartbeat driven by a thread inside the worker stops when the process does — a SIGKILL,
#: an OOM and a task timeout all take it with them, so the ordinary lease still recovers
#: those. What it does not cover is a process that is alive and *wedged*: a handler blocked
#: forever on a socket with no timeout would be heartbeated forever, and its row would stay
#: ``running`` until somebody wrote SQL against production, which invariant 10 forbids.
#:
#: So the extension is bounded. Past this the keeper stops touching, says so at ERROR, and
#: the row falls back to the ordinary stale window — a wedged worker costs one duplicated
#: run rather than a permanently stranded episode. Two hours is roughly three times the
#: longest legitimate stage yet observed (the 43-minute script job of motet#53), so a
#: healthy job never reaches it and reaching it is a signal rather than a routine event.
#:
#: **On a queue with a ``serialize_key`` the cap buys visibility rather than recovery**,
#: and that is worth not mistaking. A wedged worker still holds its advisory lock, so the
#: worker that reclaims the row finds the key busy and hands it to :func:`defer`, which
#: does not count an attempt — round and round, until the wedged process dies. Unchanged by
#: this constant and predates it; what the cap adds is the ERROR line saying which job.
MAX_LEASE_EXTENSION_SECONDS = 7200

#: The claim statement itself, hoisted out of :func:`claim` so that a test can ``EXPLAIN``
#: *this* rather than a transcription of it.
#:
#: The two arms of the ``WHERE`` clause need an index each — ``jobs_ready_idx`` from
#: migration 0001 and ``jobs_stale_idx`` from 0007 — because a ``BitmapOr`` needs an index
#: path for every arm and falls back to a sequential scan of every job ever run without
#: one. A comment on the index is what failed to notice that the first time (motet#49), so
#: the plan is asserted in ``workers/tests/test_pipeline.py`` instead. Asserting it against
#: a copy of this SQL would have reproduced the same failure one level down: the copy would
#: keep its index while the query being run drifted off it.
#:
#: Parameters, in order: the queue name, and :data:`STALE_LEASE_SECONDS`.
CLAIM_SQL = """
    UPDATE jobs
    SET state = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE queue = %s
          AND (
            (state = 'ready' AND run_at <= now())
            -- Lease reclaim. A worker that died mid-job left this row `running`
            -- and nothing else would ever pick it up. `attempts` was already
            -- incremented when it was first claimed, so the retry ceiling still
            -- bounds a job that kills every worker that touches it.
            OR (state = 'running' AND locked_at < now() - make_interval(secs => %s))
          )
        ORDER BY run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, queue, payload, attempts, serialize_key
"""


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

    Commit before running the job. ``attempts`` is incremented on claim rather than on
    failure so a job that kills its worker outright still counts toward the retry ceiling
    — otherwise a poison job retries forever.

    A ``running`` row whose lease has expired is claimable again. That is the only thing
    standing between a worker killed mid-job and a job nobody ever runs: the process that
    would have marked it done is gone, so without a lease the row is stranded and the only
    fix is manual SQL against production.

    **Expired means untouched, not merely old** — see :func:`touch`. A worker that is still
    working keeps writing ``locked_at``, so what this arm finds is a worker that has stopped
    saying anything, rather than a job that happens to be slow. Reclaiming the latter ran
    the most expensive stage in the system twice (motet#53).

    Both arms are indexed — see :data:`CLAIM_SQL`. This runs once per claim *and* once per
    queue per drain pass to discover the queue is empty, over a table nothing prunes, so it
    is the one query here where the plan is worth pinning.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(CLAIM_SQL, (queue.value, STALE_LEASE_SECONDS))
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


class LeaseTouch(Enum):
    """What :func:`touch` found when it tried to extend a lease.

    Three rather than two, because the two ways a touch can miss mean opposite things: one
    is a duplicate run in progress and the other is this job having simply finished.
    """

    #: The lease was extended. This worker still holds the job.
    HELD = "held"
    #: The row is still ``running`` under a different claim — another worker has it, and
    #: the stage is running twice. The one outcome worth an ERROR.
    LOST = "lost"
    #: The row is no longer ``running`` (or is gone): the job finished, failed, or was
    #: rescheduled. Nothing to extend and nothing wrong.
    SETTLED = "settled"


def complete(conn: psycopg.Connection[Any], job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET state = 'done', last_error = NULL, updated_at = now() WHERE id = %s",
        (job_id,),
    )


def touch(conn: psycopg.Connection[Any], job_id: int, *, attempts: int) -> LeaseTouch:
    """Push a running job's lease out, and say whether this worker still holds it.

    The counterpart to the reclaim arm of :data:`CLAIM_SQL`: that arm asks how long ago
    ``locked_at`` was written, and this rewrites it. A job that keeps saying it is alive is
    therefore never reclaimed for being slow, which is the whole of motet#53.

    **``attempts`` is a fence, and it is free** — but its uniqueness has a precondition
    worth stating, because ``claim`` incrementing the counter is not on its own enough.
    ``defer`` *decrements* it, so ``claim`` → 1, ``defer`` → 0, ``claim`` → 1 is two claims
    of one row carrying the same value. What makes the fence sound is that a deferred job
    never starts a keeper: :func:`~motet_workers.loop.drain` defers and continues the loop
    before ``_run_one``, so no worker is ever alive holding a value a later claim can
    reproduce, and every claim after the one that actually ran leaves ``attempts`` strictly
    higher. **A refactor that moved the serialization check inside the job's own execution
    would break that**, silently, and this is the sentence that says so.

    Given it, a worker whose lease *did* expire — because it was wedged past
    :data:`MAX_LEASE_EXTENSION_SECONDS`, or because it could not reach Postgres for half an
    hour — finds out, instead of quietly stamping ``locked_at`` on a row another worker is
    now running and extending the duplicate it was meant to prevent. There is no lease
    token column and this needs none: a stale worker cannot un-lose the race, but it can
    know it lost, and saying so is the difference between motet#53 and motet#53 happening
    again in silence.

    **:attr:`LeaseTouch.SETTLED` is why this returns three answers rather than a bool.** A
    touch that misses is *usually* not a lost lease at all — it is a touch that was in
    flight while its own job committed ``complete`` or ``fail``, which is a race with a
    window the width of one connect and will happen across a fleet. Reporting that as "the
    stage is running twice" would be a false alarm at ERROR, in GlitchTip, about a job that
    ran exactly once. So the miss is classified rather than assumed, which costs one
    ``SELECT`` on a path that is rare by construction.

    Deliberately **not** a fence on ``complete`` or ``fail``. Those record work that has
    already happened, and refusing to record it would strand the row rather than protect
    it; the lease is what stops the second run, not the bookkeeping afterwards.
    """
    cursor = conn.execute(
        """
        UPDATE jobs
        SET locked_at = now(), updated_at = now()
        WHERE id = %s AND state = 'running' AND attempts = %s
        """,
        (job_id, attempts),
    )
    if cursor.rowcount == 1:
        return LeaseTouch.HELD

    row = conn.execute("SELECT state FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        return LeaseTouch.SETTLED
    state = row["state"] if isinstance(row, dict) else row[0]
    return LeaseTouch.LOST if state == "running" else LeaseTouch.SETTLED


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
