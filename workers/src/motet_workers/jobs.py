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

from .queues import PIPELINE, Queue

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
#: whole stage — a 22k-token script completion and a complete Cartesia synthesis — ran
#: and billed twice for one episode. That is exactly the mistake
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
#: and that is worth not mistaking. A wedged worker still holds its advisory lock, so no
#: other worker can run that key's job while it lives — the row is stepped over by the
#: claim's busy-key filter (see :data:`CLAIM_SQL`, motet#78), where before that filter it
#: was claimed and handed to :func:`defer`, round and round, until the wedged process died.
#: The outcome is the one this paragraph always described and the churn is gone; what the
#: cap adds is still the ERROR line saying which job.
MAX_LEASE_EXTENSION_SECONDS = 7200

#: How long a ``done`` row is kept after it stops being a job (motet#56).
#:
#: **The floor is :data:`~motet_db.repo.INTEGRATED_GRACE`, and everything above it is
#: forensics.** A terminal ``done`` row carries nothing the domain rows do not already hold
#: — ``source_items.state``, ``episodes.state`` — with one exception: ``repo.list_ingestion``
#: joins a just-succeeded ``integrate`` job onto the line it keeps up for ten minutes after a
#: paste lands, so that the paste does not vanish from one list and reappear in another under
#: a title dedup rewrote. Anything older than that grace is dead weight to the only reader
#: that wants it.
#:
#: **Be exact about what a window under the grace would cost, because it is less than it
#: sounds and the accuracy is the point.** That arm is driven by ``source_items`` and joins
#: the job *left*, so deleting the row does not remove the line — it empties it: the attempt
#: count reads zero and any job-side error goes with it, on the one line somebody is at that
#: moment watching. Blanking a field on a panel is not what the ``failed`` window below is
#: guarding against, and conflating the two would be an argument for making that one shorter.
#:
#: Seven days rather than eleven minutes because the row is still the only record of *when a
#: stage ran and how many attempts it took* — the domain rows carry the end state, not the
#: history — and the realistic question is "something looked wrong in Monday's briefing",
#: asked on Friday. A week is a thousand times the grace it has to outlive and still bounds
#: the table by throughput instead of by the deployment's age, which is the whole of what
#: this fixes.
DONE_RETENTION_SECONDS = 7 * 24 * 3600

#: How long a ``failed`` row is kept. Materially longer than :data:`DONE_RETENTION_SECONDS`,
#: and the asymmetry is the decision rather than caution.
#:
#: **A ``failed`` row's ``last_error`` is the only copy of why a job stopped being retried,
#: and for two queues it is the only copy of anything.** ``failure_recorders`` has no entry
#: for ``poll`` or ``extract``, because neither has a domain object to mark: a mailbox
#: message that could not be fetched has no ``source_items`` row, extraction is what would
#: have written one, and ``handle_poll`` has already advanced the cursor past it. The failed
#: job row *is* the record that the message was ever seen (motet#35) — and
#: ``repo.list_ingestion``'s extract arm, which is what puts it on the user's screen, has no
#: time bound of its own, so a deleted row does not age out of that panel, it disappears
#: from it.
#:
#: That is the quiet failure this window is set against: too short, and a lost newsletter
#: stops being reported with nothing anywhere saying it was. A quarter is far longer than
#: anyone leaves a backlog unattended, and costs nothing in rows — a ``failed`` row means
#: five attempts were exhausted, which is rare by construction and is not the volume line.
FAILED_RETENTION_SECONDS = 90 * 24 * 3600

#: Rows deleted per statement by :func:`prune`.
#:
#: **The bound is the point.** An unbounded ``DELETE`` on a queue table holds every row lock
#: it takes until it commits, against the claim query the pruning exists to help. A batch is
#: one statement on an autocommit connection, so the locks are released a thousand rows at a
#: time and a sweep is interruptible at every boundary.
PRUNE_BATCH_SIZE = 1000

#: The most batches one :func:`prune` call runs, per state. The second half of the bound: a
#: sweep is work an operator has not asked for, sharing a database with jobs somebody is
#: waiting on, so it does a fixed amount and leaves the rest to the next one. Reaching it is
#: not an error — with a sweep every :data:`~motet_workers.runner.PRUNE_INTERVAL_SECONDS` a
#: backlog drains steadily — but it is worth a line, because a cap hit every hour forever is
#: the table growing faster than this drains it.
PRUNE_MAX_BATCHES = 10

#: One batch of the retention sweep, hoisted out of :func:`prune` for the same reason
#: :data:`CLAIM_SQL` is: a test can ``EXPLAIN`` *this* rather than a transcription of it,
#: and a transcription is what keeps its index while the statement being run drifts off it.
#:
#: One state per call, not ``state IN ('done', 'failed')`` with a ``CASE`` over the windows:
#: the two states have different windows for different reasons, and an ``OR`` would need an
#: index path per arm to avoid the sequential scan this is designed around (motet#49).
#:
#: ``FOR UPDATE SKIP LOCKED`` over a row the claim query cannot return — its subquery
#: matches only ``ready`` and ``running`` — so this is belt-and-braces against a future
#: writer rather than load-bearing today. It costs nothing and it means a sweep can never be
#: the thing a worker is waiting behind.
#:
#: Parameters, in order: the state, its retention window in seconds, and the batch size.
PRUNE_SQL = """
    DELETE FROM jobs
    WHERE id IN (
        SELECT id FROM jobs
        WHERE state = %s
          AND updated_at < now() - make_interval(secs => %s)
        ORDER BY updated_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
"""

#: The retention windows, by the state each applies to. Iterated by :func:`prune`, so a
#: state added to the ``CHECK`` constraint and not to this mapping is simply not swept —
#: which is the right default for a statement whose whole content is deletion.
RETENTION_SECONDS: Mapping[str, int] = {
    "done": DONE_RETENTION_SECONDS,
    "failed": FAILED_RETENTION_SECONDS,
}

#: Every advisory-lock key held right now, as the bigint :func:`lock_key` produced.
#:
#: ``pg_locks`` splits a one-argument advisory lock back into the two 32-bit halves it was
#: passed as — the high half in ``classid``, the low in ``objid``, both typed ``oid`` and
#: therefore *unsigned*. Reassembling them arithmetically overflows ``bigint`` for any key
#: with the top bit set, which is half of them, so the halves are concatenated as bit
#: strings and read back as two's complement. ``objsubid = 1`` is the one-bigint form;
#: ``2`` would be ``pg_advisory_lock(int, int)``, which nothing here uses.
#:
#: ``database`` is not decoration. ``pg_locks`` is cluster-wide while an advisory lock is
#: per database, so without it a worker would skip work because *another database on the
#: same server* held a key that happens to collide — which is every CI run beside a local
#: one, since each pytest run creates a database of its own.
#:
#: ``granted``, because a lock somebody is queued for is not a lock anybody holds. Nothing
#: here ever waits (:func:`try_lock` is ``pg_try_advisory_lock``), so this is belt and
#: braces rather than load-bearing.
#:
#: **"Held by anybody", not "held by somebody else", and both readers want it that way.**
#: :func:`~motet_workers.loop.drain` releases its lock in a ``finally`` before its next
#: claim, so a claiming worker never meets its own and a ``pid`` filter would buy it
#: nothing; and :attr:`QueueReadiness.blocked_keys` reads this same statement precisely to
#: report "somebody is on this key", which includes the pass asking. A future change that
#: held a key *across* claims — batching one user's jobs, judging them in parallel — would
#: make a worker hide the rows it is working on, with no test failing, and ``AND l.pid <>
#: pg_backend_pid()`` is the one-line answer if that day comes. Note what it would *not*
#: do, because the name invites the mistake: ``pg_backend_pid()`` identifies a
#: **connection**, not an OS process, so a second connection of this same worker survives
#: the filter untouched.
#:
#: A role that cannot read ``pg_locks`` or ``pg_database`` makes every reader of this
#: statement raise, and they degrade differently: :func:`claim` has no fail-open path and
#: would stop every queue, :func:`~motet_workers.loop._record_readiness` swallows, and
#: ``/v1/processing`` would 500 — which the SPA already renders as its "could not ask"
#: state rather than as an idle pipeline. Both views are readable by any role on a stock
#: Postgres and on Cloud SQL, so this is a claim about the estate that nothing here can
#: test; staging is where it is settled.
HELD_LOCK_KEYS_SQL = """
    SELECT ((l.classid::bigint::bit(64) << 32) | l.objid::bigint::bit(64))::bigint AS key
    FROM pg_locks l
    WHERE l.locktype = 'advisory'
      AND l.objsubid = 1
      AND l.granted
      AND l.database = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database())
"""

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
#: **The ``lock_key`` arm is the per-user fairness filter** (motet#78), and three things
#: about how it is written are the whole of it:
#:
#: * It is a *read* of ``pg_locks``, never a call to ``pg_try_advisory_lock``. A function
#:   with side effects in a ``WHERE`` can fire for candidate rows that ``SKIP LOCKED`` then
#:   throws away, and it would take the lock before the claim — while the lease fence's
#:   soundness rests on "a deferred job never starts a keeper", which the claim-then-lock
#:   order in :func:`~motet_workers.loop.drain` is what guarantees. That order is untouched:
#:   this is an optimisation, and :func:`try_lock` is still the correctness fence. A key
#:   taken in the window between the two still ends in :func:`defer`, exactly as before.
#: * ``NOT EXISTS`` rather than ``NOT IN``, and the difference is a queue that stops. A
#:   single NULL anywhere in a ``NOT IN`` list makes the predicate NULL for *every* row, so
#:   one unexpected row in ``pg_locks`` would silently offer nothing on any queue — and a
#:   queue nothing is returned from looks exactly like a queue with nothing in it. It is
#:   also what makes a row whose ``lock_key`` is NULL read as *not held* rather than as
#:   held: a row written before migration 0011 and outside its backfill is still offered,
#:   and still goes through :func:`try_lock`. The two directions are not symmetric —
#:   leaning this way costs the claim-and-defer cycle this exists to remove, and leaning
#:   the other way strands work permanently and quietly.
#: * ``lock_key IS NULL OR ...`` is therefore not needed for correctness, and it is here
#:   for cost: it short-circuits, so on the four queues that carry no serialization key at
#:   all — ``extract``, ``assemble``, ``script``, ``tts`` — the subplan is *never executed*
#:   and ``pg_locks`` is never read. ``pg_lock_status()`` takes every lock-manager
#:   partition lock to build its answer, which is not a thing to do once a claim on a queue
#:   that can never need it. ``EXPLAIN (ANALYZE)`` says "never executed" for those queues,
#:   and ``workers/tests/test_pipeline.py`` asserts it.
#:
#: Parameters, in order: the queue name, and :data:`STALE_LEASE_SECONDS`.
CLAIM_SQL = f"""
    UPDATE jobs
    SET state = 'running', locked_at = now(), attempts = attempts + 1, updated_at = now()
    WHERE id = (
        SELECT j.id FROM jobs j
        WHERE j.queue = %s
          AND (
            (j.state = 'ready' AND j.run_at <= now())
            -- Lease reclaim. A worker that died mid-job left this row `running`
            -- and nothing else would ever pick it up. `attempts` was already
            -- incremented when it was first claimed, so the retry ceiling still
            -- bounds a job that kills every worker that touches it.
            OR (j.state = 'running' AND j.locked_at < now() - make_interval(secs => %s))
          )
          -- Invariant 6's serialization, read a step earlier: do not offer a row whose
          -- user is already being worked on. Without it a burst of one user's jobs at the
          -- head of the queue is claimed and deferred by every other worker in turn,
          -- once per row, before any of them reaches anyone else's work.
          AND (
            j.lock_key IS NULL
            OR NOT EXISTS (SELECT 1 FROM ({HELD_LOCK_KEYS_SQL}) held WHERE held.key = j.lock_key)
          )
        ORDER BY j.run_at, j.id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, queue, payload, attempts, serialize_key, work_committed_attempt
"""


@dataclass(frozen=True)
class Job:
    id: int
    queue: Queue
    payload: Mapping[str, Any]
    attempts: int
    serialize_key: str | None
    #: Which earlier attempt's work is already committed, or None if none is.
    #:
    #: Set by :func:`mark_work_committed` inside the handler's own transaction, so a
    #: non-NULL value here on a freshly claimed job means exactly one thing: this row's
    #: work landed and the worker died before it could say so. The runner completes such a
    #: job rather than running it again — see :func:`mark_work_committed` for why the fence
    #: lives on the job rather than on the domain object it wrote.
    work_committed_attempt: int | None


def enqueue(
    conn: psycopg.Connection[Any],
    queue: Queue,
    payload: Mapping[str, Any],
    *,
    serialize_key: str | None = None,
    delay_seconds: int = 0,
) -> int:
    """Add a job. Call inside the transaction that creates the work it refers to.

    ``lock_key`` is derived here rather than left to the claim, so that the claim can read
    it off the row instead of hashing every candidate (motet#78). This is the only place it
    is ever written: it is a pure function of ``serialize_key``, which nothing updates, so
    there is no second writer to keep in step. The one other producer is migration 0011's
    backfill, which runs once and is pinned against :func:`lock_key` by a test.
    """
    row = conn.execute(
        """
        INSERT INTO jobs (queue, payload, serialize_key, lock_key, run_at)
        VALUES (%s, %s::jsonb, %s, %s, now() + make_interval(secs => %s))
        RETURNING id
        """,
        (
            queue.value,
            json.dumps(dict(payload)),
            serialize_key,
            None if serialize_key is None else lock_key(serialize_key),
            delay_seconds,
        ),
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
    queue per drain pass to discover the queue is empty, over a table whose size is now
    bounded by :func:`prune`'s retention windows rather than by the deployment's age — days
    of every stage's traffic, which is still orders of magnitude more rows than this query
    wants. So it remains the one query here where the plan is worth pinning.
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
        work_committed_attempt=row["work_committed_attempt"],
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


def mark_work_committed(conn: psycopg.Connection[Any], job_id: int, *, attempts: int) -> None:
    """Record that this attempt's work is applied — **from inside the work's transaction**.

    This is the fence of motet#55, and where it is called from is the whole of it. Written
    in the same transaction as the handler's own writes, it is durable exactly when they
    are: a row carrying it has done its work, and a row without it has not. Written
    anywhere else it would be a second opinion about the same fact, which is what the
    problem already was.

    **The window it closes is the one :func:`complete` cannot.** ``_execute`` commits the
    handler's work and the job's outcome separately, because the failure path has to record
    ``jobs.fail`` on a connection whose work transaction has just aborted and cannot do that
    from inside it. So a worker that dies between the two leaves the row ``running`` with
    the work durably applied, and :data:`STALE_LEASE_SECONDS` later another worker claims
    it. That reclaim is the recovery a killed worker depends on and must stay — what must
    not happen is the stage running a second time, which for ``script`` means another billed
    completion and a ``failed`` episode silently put back into ``rendering`` with the
    ``last_error`` that said why it failed overwritten (motet#55).

    **Why the fence is on the job and not on the episode.** Every handler already
    short-circuits its own finished work by reading domain state, and for ``script`` that
    guard cannot close this: the episode may legitimately be ``failed`` by the time the
    stale row is claimed, and a re-script somebody *asked for* arrives looking exactly the
    same. Widening the state check would trade this defect for its quiet twin — an episode
    stranded in ``failed`` with no TTS job and nothing alerting on it. The job row is where
    the two are distinguishable, because a deliberate re-script is a *different row*, with
    this column NULL, and runs.

    **Not a substitute for the handlers' own idempotence.** A concurrent double-run — a
    worker wedged past :data:`MAX_LEASE_EXTENSION_SECONDS`, whose row is reclaimed while it
    is still working — has two live claims, and neither can see the other's uncommitted
    work. This is a fence against *replay*; the lease (motet#53) is what bounds concurrency,
    and the state guards are what make a converging re-run harmless.

    ``attempts`` rather than a flag, because it says *which* claim's work landed, which is
    what makes the log line the runner writes worth reading.

    **It also sets a lock order, and everything else has to keep to it: a domain row
    first, then the job row.** This is the only write that takes the job's own row lock
    from inside a handler's transaction, and it is deliberately the *last* statement — a
    fence written at the top would hold that lock for the whole of a forty-minute stage
    and block the lease keeper, which is motet#53 reintroduced. The consequence is that
    ``_execute``'s failure arm, which touches both rows too, must take them in the same
    order (it records the domain object, then calls :func:`fail`); the other way round two
    workers on one row — the concurrent case the lease bounds but does not eliminate —
    could deadlock, and the loser is whichever of them Postgres picks.
    """
    conn.execute(
        "UPDATE jobs SET work_committed_attempt = %s, updated_at = now() WHERE id = %s",
        (attempts, job_id),
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


def will_retry(job: Job, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    """Whether :func:`fail` would reschedule this job rather than give up on it.

    Exposed so that the runner can decide *before* calling ``fail`` whether the domain
    object is about to be marked failed, and write it first. That ordering matters — see
    the lock-order note in :func:`mark_work_committed` — and the predicate is here so that
    the ceiling has one definition rather than two that can drift apart.
    """
    return job.attempts < max_attempts


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
    if not will_retry(job, max_attempts=max_attempts):
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


@dataclass(frozen=True)
class Pruned:
    """What one retention sweep deleted, and whether it ran out of budget doing it."""

    #: Rows deleted, by state. Every swept state is present, at zero if nothing matched —
    #: "nothing to delete" and "the sweep never ran" are different facts and the never-infer-
    #: "no errors"-from-"no data" trap in AGENTS.md is what happens when they share a shape.
    #: **Empty means the sweep failed**, which is a third thing again and is why
    #: :func:`~motet_workers.loop.prune_jobs` reports its outcome on a counter of its own
    #: rather than leaving a fault to be inferred from a missing row count.
    deleted: Mapping[str, int]
    #: Whether any state used its whole batch budget — every batch full, none short.
    #:
    #: **A lower-bound signal, not a count of what is left**, and wrong at both edges in the
    #: direction that costs nothing. A sweep that empties the window in exactly its whole
    #: budget says ``True`` with nothing remaining; and a batch cut short by ``SKIP LOCKED``
    #: — two sweeps at once is the ordinary deployment, an always-on worker beside a
    #: per-enqueue execution — breaks the loop and says ``False`` with rows left. Both are
    #: benign because the next sweep settles it; what this is for is the case that does not
    #: settle, a budget exhausted every sweep forever.
    capped: bool

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


def prune(
    conn: psycopg.Connection[Any],
    *,
    batch_size: int = PRUNE_BATCH_SIZE,
    max_batches: int = PRUNE_MAX_BATCHES,
) -> Pruned:
    """Delete terminal job rows past their retention window, in bounded batches.

    Nothing had ever deleted a job row (motet#56). ``complete`` flips the state to ``done``
    and the row stays, so the table grew for the life of the deployment: one row per
    pipeline stage per pasted item and per episode, forever. The consequence left after
    motet#49's index is storage, autovacuum work, and the size of every *other* index on the
    table — ``jobs_source_item_idx`` holds every ``integrate`` job ever run and is walked by
    the ingestion panel the SPA polls while anything is pending.

    **Two windows, because the two terminal states hold different amounts of information.**
    A ``done`` row is redundant with the domain rows past
    :data:`~motet_db.repo.INTEGRATED_GRACE`; a ``failed`` row carries ``last_error``, which
    for ``poll`` and ``extract`` is the only record anywhere that a mailbox message was seen
    and lost. See :data:`DONE_RETENTION_SECONDS` and :data:`FAILED_RETENTION_SECONDS` — the
    windows are where the reasoning is, and the short one is the one that fails quietly.

    **Call this on an autocommit connection, and it refuses otherwise.** The batching is
    the entire bound, and inside one transaction it would not be one: every batch's row
    locks would be held until the last of them committed, which is an unbounded ``DELETE``
    with extra steps. Autocommit makes each statement its own transaction, so the sweep
    holds at most ``batch_size`` row locks at a time and can be abandoned between batches
    without rolling anything back.

    The check is a ``ValueError`` rather than a docstring because a violated precondition
    here is invisible: the sweep deletes exactly the same rows either way and every
    assertion about *which* rows still passes, so the one property the design rests on could
    be dropped without a single test going red. The caller that has to get this right is
    :func:`~motet_workers.loop.prune_jobs`, and this is what says so at the moment it is
    wrong rather than in production.

    ``updated_at`` is the age, and it is the right column because ``complete`` and ``fail``
    both write it when the row reaches its terminal state and nothing writes it afterwards.
    ``created_at`` would be when the job was *enqueued*, which for a job retried up the
    backoff ladder is most of an hour earlier, and ``id`` is not a clock at all.

    Returns what it deleted, so the caller can say so — see
    :func:`~motet_workers.loop.prune_jobs`, which is where the metric and the log line live
    for the same reason the rest of the telemetry does: this module takes connections and
    never opens one, and is not where a decision about observability belongs.
    """
    if not conn.autocommit:
        raise ValueError(
            "prune() needs an autocommit connection: inside one transaction the batches "
            "hold every row lock until the last of them commits, which is the unbounded "
            "DELETE the batching exists to avoid"
        )

    deleted: dict[str, int] = {}
    capped = False
    for state, seconds in RETENTION_SECONDS.items():
        removed = 0
        for _ in range(max_batches):
            cursor = conn.execute(PRUNE_SQL, (state, seconds, batch_size))
            removed += cursor.rowcount
            # A short batch means there is no point asking again, and asking is a scan.
            # Usually that means the window is drained; under a concurrent sweep it can
            # instead mean `SKIP LOCKED` passed over rows the other one holds, which is why
            # `capped` is documented as a lower bound. Only running every batch to the full
            # size reaches the `else` below — "stopped on budget" rather than "stopped on
            # rows", the one of the two worth a line.
            if cursor.rowcount < batch_size:
                break
        else:
            capped = True
        deleted[state] = removed
    return Pruned(deleted=deleted, capped=capped)


def queue_depths(conn: psycopg.Connection[Any]) -> dict[str, dict[str, int]]:
    """Ready/running/failed counts per queue — what a health check reports."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT queue, state, count(*) AS n FROM jobs GROUP BY queue, state")
        rows = cur.fetchall()
    depths: dict[str, dict[str, int]] = {}
    for row in rows:
        depths.setdefault(row["queue"], {})[row["state"]] = row["n"]
    return depths


@dataclass(frozen=True)
class QueueReadiness:
    """How much work one queue has that could start now, and how many workers could take it.

    Two numbers rather than one, because **queue depth is the wrong scaling signal for a
    serialized queue** (motet#78). Two thousand ready ``integrate`` rows for one user can
    employ exactly one worker — invariant 6 says so, and the advisory lock enforces it — so
    a scaler reading ``ready`` would start a pool that spends its life deferring.
    """

    queue: str
    #: Rows that are ``ready`` and due. The right signal for a queue with no serialization:
    #: ``extract``, ``assemble``, ``script`` and ``tts`` parallelize freely, and a scaler
    #: wants ``ceil(ready / target_per_worker)`` of them.
    #:
    #: **Ready and due only, so it is zero while the queue's whole backlog is ``running``**,
    #: and a scaler reading it alone would scale a pool to zero on top of a job that is
    #: still going. That is why motet#78 specifies a **floor of one** wherever a heartbeat
    #: is fresh, and the floor is the deployment's half of this signal rather than an
    #: oversight in it: this number answers "how much work is waiting", and
    #: ``worker_heartbeats`` answers "is anyone on it".
    ready: int
    #: How many of those rows could be worked on **at the same time** — the number of
    #: workers this queue could keep busy, and the answer for ``integrate`` and ``poll``.
    #:
    #: Distinct serialization keys, **plus one for each row that has no key**. The issue
    #: specifies ``count(DISTINCT serialize_key)`` for the serialized queues and a plain row
    #: count for the others; this expression is equal to whichever of those applies, on
    #: every queue that exists, because today a queue's rows either all carry a key or none
    #: of them does. What it adds is that a queue carrying *both* reports a number a scaler
    #: can use, instead of a zero that reads as "no work" — the
    #: never-infer-"no errors"-from-"no data" trap in AGENTS.md, on the one series a scaler
    #: would act on.
    ready_keys: int
    #: How many of those keys are, at this instant, **already held by somebody** — so the
    #: work is waiting on a worker that has it rather than on a worker that does not exist.
    #:
    #: This exists because the busy-key filter in :data:`CLAIM_SQL` took a signal away. A
    #: worker that met a held key used to claim the row, log "job N deferred", and defer it;
    #: the churn was the defect, and the log line was the only evidence anywhere that a key
    #: was blocking work. Stepping the row over silently would leave a *leaked* lock — a
    #: wedged worker past :data:`MAX_LEASE_EXTENSION_SECONDS`, a session that never released
    #: — looking exactly like an idle deployment: workers claiming nothing, ``ready_keys``
    #: saying "start more workers", and not a line anywhere. Same argument as
    #: ``motet.jobs.lease{outcome="held"}``.
    #:
    #: **Nonzero is the healthy case, not the alarm.** A key is held whenever somebody is
    #: working it, which is what the whole mechanism is for. What is worth an operator's
    #: attention is this staying pinned while ``ready`` does not fall.
    blocked_keys: int


#: Work that could start right now, per queue, with keyed rows counted once per key.
#:
#: ``run_at <= now()`` is what "due" means, and leaving it out would be the whole of the
#: signal's usefulness: a queue full of rows backing off up the retry ladder, or deferred
#: five seconds because a key was busy, is a queue with nothing for a new worker to do.
#:
#: **Two aggregates rather than ``count(DISTINCT serialize_key)``, and the reason is the
#: plan.** ``count(DISTINCT)`` cannot hash-aggregate, so it plans as a ``GroupAggregate``
#: over a ``Sort`` of every due row; the shape below groups by the key once and counts the
#: groups, which is two nested ``HashAggregate``\ s. Measured over 20,000 due rows, median
#: of eleven: 83 ms against 20 ms. This runs on every drain pass *and* on every
#: ``/v1/processing``, which the SPA polls every three seconds while anything is pending —
#: which is exactly during the burst this signal is about.
#:
#: ``max(lock_key)`` rather than grouping by it: the two columns are one-to-one, and taking
#: the key as an aggregate means a row that somehow disagreed could not split one user into
#: two groups and count them twice. ``max`` also skips NULLs, which is what decides the
#: rolling-deploy window migration 0011 describes — a key whose rows are half backfilled
#: resolves to the real key and reads as blocked, and a key with no ``lock_key`` on any of
#: its rows reads as *not* blocked. The second is the same fail-open the claim makes, and
#: it is worth knowing it costs the operator signal below for the length of the deploy.
QUEUE_READINESS_SQL = f"""
    WITH held AS ({HELD_LOCK_KEYS_SQL}),
    due AS (
        SELECT queue, serialize_key, max(lock_key) AS lock_key, count(*) AS n
        FROM jobs
        WHERE state = 'ready' AND run_at <= now()
        GROUP BY queue, serialize_key
    )
    SELECT queue,
           sum(n)::bigint AS ready,
           -- One per key, plus one per keyless row: see QueueReadiness.ready_keys. The
           -- coalesce is for a queue with no keyless rows at all, where the sum is NULL.
           (count(*) FILTER (WHERE serialize_key IS NOT NULL)
            + coalesce(sum(n) FILTER (WHERE serialize_key IS NULL), 0))::bigint AS ready_keys,
           count(*) FILTER (
               WHERE serialize_key IS NOT NULL
                 AND EXISTS (SELECT 1 FROM held WHERE held.key = due.lock_key)
           )::bigint AS blocked_keys
    FROM due
    GROUP BY queue
"""


def queue_readiness(conn: psycopg.Connection[Any]) -> list[QueueReadiness]:
    """Per queue, in pipeline order: how much is due, and how many workers could take it.

    **Every queue is returned, at zero when it has nothing** — a queue missing from the
    result would be indistinguishable from a queue nobody asked about, and this is read by
    a gauge and by ``/v1/processing``, which are both surfaces where an absent series reads
    as "fine". Same argument as the worker heartbeat one module over (motet#38).

    A queue name in the table that is not in :data:`~motet_workers.queues.PIPELINE` is
    dropped rather than reported: nothing drains it, so it is not a scaling signal, and the
    alternative is a metric label an unknown string can mint a time series under.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(QUEUE_READINESS_SQL)
        rows = {row["queue"]: row for row in cur.fetchall()}
    return [
        QueueReadiness(
            queue=queue.value,
            ready=rows[queue.value]["ready"] if queue.value in rows else 0,
            ready_keys=rows[queue.value]["ready_keys"] if queue.value in rows else 0,
            blocked_keys=rows[queue.value]["blocked_keys"] if queue.value in rows else 0,
        )
        for queue in PIPELINE
    ]
