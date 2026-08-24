"""Worker entry point — one Cloud Run job per queue.

A worker takes a queue name, drains it, and exits. It is never a long-lived server,
because Cloud Run jobs are how the pipeline stages are scheduled — and because a process
that exits cannot leak a connection, a lock, or a half-finished transaction across runs.

**The transaction boundaries here are the interesting part**, and they are three, not one:

1. *Claim*, committed immediately. The job is marked ``running`` before any work starts,
   so a worker that dies mid-job leaves a visible row rather than a job that silently
   reappears.
2. *The work itself*, committed as a unit. A handler writes a news item, its link row, and
   the source item's new state together — or writes none of them.
3. *The outcome*, in its own transaction. Recording "this succeeded" must not be able to
   fail because the work rolled back, and must not roll back because recording failed.

Squashing these into one transaction is the obvious simplification and it is wrong: a
handler failure would roll back the attempt counter along with the work, and a poison job
would then retry forever.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any

import psycopg
from motet_db import repo
from motet_inference import get_stages, validate_tts_startup
from motet_inference.llm import validate_startup as validate_llm_startup
from motet_inference.mode import current_mode
from motet_storage import build_store

from . import jobs
from .handlers import HANDLERS, Context, PermanentFailure, failure_recorders
from .queues import Queue

logger = logging.getLogger("motet.worker")

#: A safety stop on one invocation, so a runaway producer cannot keep a Cloud Run job
#: alive indefinitely. Reaching it is not an error — the next scheduled run continues.
MAX_JOBS_PER_RUN = 500


def drain(queue: Queue, database_url: str, *, max_jobs: int = MAX_JOBS_PER_RUN) -> int:
    """Claim and run every ready job on ``queue``. Returns the number processed."""
    handler = HANDLERS.get(queue)
    if handler is None:
        raise ValueError(
            f"queue {queue.value!r} has no handler in Phase 1. `poll` and `extract` belong "
            "to Gmail and X ingestion, which is Phase 2 — see AGENTS.md."
        )
    stages = get_stages()
    store = build_store()
    recorders = failure_recorders()
    processed = 0
    deferred = 0

    with repo.connect(database_url) as conn:
        conn.autocommit = True
        while processed + deferred < max_jobs:
            job = jobs.claim(conn, queue)
            if job is None:
                break

            # Invariant 6. The lock is taken *after* the claim so that a busy key does not
            # block other jobs on the same queue from being claimed at all — this worker
            # simply hands this one back and looks for different work.
            if job.serialize_key is not None and not jobs.try_lock(conn, job.serialize_key):
                logger.info(
                    "job %d deferred: %r is already being processed", job.id, job.serialize_key
                )
                jobs.defer(conn, job)
                # Bounded separately from `processed`, which means "jobs that ran" and is
                # what this function returns. A deferral is cheap but not free, and a queue
                # backed up behind one busy key would otherwise let a single pass touch
                # every row in it with no ceiling at all.
                deferred += 1
                continue

            try:
                _run_one(conn, job, handler, stages, store, recorders)
            finally:
                if job.serialize_key is not None:
                    jobs.unlock(conn, job.serialize_key)
            processed += 1

    if deferred:
        logger.info(
            "deferred %d job(s) on %s whose serialization key was busy elsewhere",
            deferred,
            queue.value,
        )
    if processed + deferred >= max_jobs:
        logger.warning(
            "stopped after %d jobs on %s; more may be ready and the next run will take them",
            processed,
            queue.value,
        )
    logger.info("drained %d job(s) from %s", processed, queue.value)
    return processed


def _run_one(
    conn: psycopg.Connection[Any],
    job: jobs.Job,
    handler: Any,
    stages: Any,
    store: Any,
    recorders: Any,
) -> None:
    """Run one job's handler, then record the outcome in a separate transaction."""
    context = Context(conn=conn, stages=stages, store=store)
    try:
        with conn.transaction():
            handler(context, job.payload)
    except PermanentFailure as exc:
        # Retrying cannot help, so skip the backoff ladder entirely and surface it now.
        message = f"{type(exc).__name__}: {exc}"
        with conn.transaction():
            jobs.fail(conn, job, message, max_attempts=0)
            _record_failure(conn, job, recorders, message)
        return
    except Exception as exc:  # noqa: BLE001 — the queue's whole job is to survive these
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("job %d on %s raised", job.id, job.queue.value)
        with conn.transaction():
            will_retry = jobs.fail(conn, job, message)
            if not will_retry:
                _record_failure(conn, job, recorders, message)
        return

    with conn.transaction():
        jobs.complete(conn, job.id)


def _record_failure(
    conn: psycopg.Connection[Any], job: jobs.Job, recorders: Any, message: str
) -> None:
    """Mark the *domain* object failed once the job has stopped being retried.

    Separated from the job row so that a user sees "this episode failed, here is why" on
    the episode itself rather than having to be shown the queue.
    """
    recorder = recorders.get(job.queue)
    if recorder is not None:
        recorder(conn, job.payload, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain one Motet pipeline queue.")
    parser.add_argument("queue", choices=[q.value for q in Queue])
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.0,
        help=(
            "Keep draining, sleeping this long between empty passes. Default 0 drains "
            "once and exits, which is what a Cloud Run job wants."
        ),
    )
    parser.add_argument("--max-jobs", type=int, default=MAX_JOBS_PER_RUN)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Before anything else, and before claiming any job. A Cloud Run job that cannot reach
    # a model should fail immediately and visibly, rather than claim work it will then fail
    # to finish — and it should report *that* rather than whichever check happens to run
    # first. Ordering this ahead of the database check keeps the credential failure the one
    # you see when both are wrong.
    logger.info("llm: %s", validate_llm_startup().describe())

    queue = Queue(args.queue)
    if queue is Queue.TTS and current_mode() == "real":
        # Only the worker that actually speaks needs the TTS credentials, and it needs them
        # before it claims a job rather than after it has been handed a scripted episode.
        validate_tts_startup()
        logger.info("tts: credentials resolved")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is not set")

    if args.poll_seconds <= 0:
        drain(queue, database_url, max_jobs=args.max_jobs)
        return 0

    while True:
        if drain(queue, database_url, max_jobs=args.max_jobs) == 0:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
