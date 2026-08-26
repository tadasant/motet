"""The worker loop — claim jobs off one queue, run them, record how each went.

A worker takes a queue name, drains it, and exits. It is never a long-lived server,
because Cloud Run jobs are how the pipeline stages are scheduled — and because a process
that exits cannot leak a connection, a lock, or a half-finished transaction across runs.

**This is the importable half; the entry point is ``motet_workers.runner``**, and the two
are separate modules on purpose. The package's ``__init__`` re-exports :func:`drain`, so
whichever module holds it is imported the moment anything touches ``motet_workers`` — and
``runner`` is the module ``python -m`` *executes*. A module that is both imported and
executed runs its top level twice, under two names, with two copies of every module-level
object, which is what ``runpy`` means by "may result in unpredictable behaviour". Keeping
the loop here and the CLI there is what makes that impossible rather than merely absent.
See motet#21, and ``workers/tests/test_entrypoint.py``, which fails if it comes back.

``loop`` rather than ``drain``, so that ``motet_workers.drain`` means the function and
only the function. Had the module been named after it, ``from .drain import drain`` in
``__init__`` would leave the package attribute bound to the function while
``sys.modules`` still held the module under that path — and
``monkeypatch.setattr("motet_workers.drain.MAX_JOBS_PER_RUN", 1, raising=False)`` would
then set an attribute on the *function object* and patch nothing, silently.

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

import logging
import time
from typing import Any

import psycopg
from motet_db import repo
from motet_inference import Stages, get_stages
from motet_inference.llm import LlmBudgetExhaustedError
from motet_storage import ObjectStore, build_store
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from . import jobs
from .handlers import HANDLERS, Context, PermanentFailure, failure_recorders
from .queues import Queue

logger = logging.getLogger("motet.worker")

# Created at import against OpenTelemetry's *proxy* providers, which resolve to the real
# ones the moment `motet_obs.configure` installs them. That is what lets telemetry stay
# entirely optional: with nothing configured these are no-ops, and no code path here has
# to ask whether obs exists.
_tracer = trace.get_tracer("motet.worker")
_meter = metrics.get_meter("motet.worker")

#: The worker's two numbers. Everything an operator wants to know about a queue — is it
#: draining, is it failing, is it slow — is one of these split by its attributes, which is
#: why they are a counter and a histogram rather than a gauge per queue.
_jobs_processed = _meter.create_counter(
    "motet.jobs.processed",
    unit="{job}",
    description="Jobs taken off a queue, by queue and outcome.",
)
_job_duration = _meter.create_histogram(
    "motet.job.duration",
    unit="ms",
    description="Wall-clock time a job's handler took, by queue and outcome.",
)

#: A safety stop on one invocation, so a runaway producer cannot keep a Cloud Run job
#: alive indefinitely. Reaching it is not an error — the next scheduled run continues.
MAX_JOBS_PER_RUN = 500


def drain(
    queue: Queue,
    database_url: str,
    *,
    max_jobs: int = MAX_JOBS_PER_RUN,
    stages: Stages | None = None,
    store: ObjectStore | None = None,
) -> int:
    """Claim and run every ready job on ``queue``. Returns the number processed.

    **A long-lived worker passes ``stages`` and ``store`` in, and that is not an
    optimisation.** Resolving them here is right for a Cloud Run job, which drains once
    and exits — but ``runner all --poll-seconds N`` calls this several times a second, and
    ``real_stages()`` mints a fresh ``LlmClient`` every time it is called. OpenRouter's
    sticky upstream routing is *per client*, and that routing is what keeps the dedup
    prompt cache warm — the largest LLM cost lever in the system (see AGENTS.md). A client
    per pass would throw the cache away on every sweep and leak a connection pool doing it.

    They stay optional so that a one-shot drain, and every test that calls this, needs to
    know none of it.
    """
    handler = HANDLERS.get(queue)
    if handler is None:
        raise ValueError(f"queue {queue.value!r} has no handler registered")
    stages = get_stages() if stages is None else stages
    store = build_store() if store is None else store
    recorders = failure_recorders()
    processed = 0

    with (
        _tracer.start_as_current_span(
            f"drain {queue.value}", attributes={"motet.queue": queue.value}
        ) as run,
        repo.connect(database_url) as conn,
    ):
        conn.autocommit = True
        # Before claiming anything, and whether or not there is anything to claim. "A
        # worker is running" is what an empty pass proves, and it is the fact motet#38
        # turned on: with no heartbeat, a queue nothing is draining looks exactly like a
        # queue that is draining fine and has nothing to do.
        repo.record_worker_heartbeat(conn, queue.value)
        while processed < max_jobs:
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
                continue

            try:
                _run_one(conn, job, handler, stages, store, recorders)
            finally:
                if job.serialize_key is not None:
                    jobs.unlock(conn, job.serialize_key)
            processed += 1

        # Inside the `with`, because setting an attribute on an ended span is silently
        # dropped — the span closes when this block does.
        run.set_attribute("motet.jobs.processed", processed)

    if processed >= max_jobs:
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
    """Run one job under a span, and record how it went as a metric.

    The span and the two instruments are the worker's whole observability surface. It has
    no health route to ask and it is the process that spends the money, so "did the
    integrate queue drain, and how long did Cartesia take" has to be answerable from
    outside — which before this it was not, from anywhere.
    """
    attributes = {"motet.queue": job.queue.value}
    started = time.perf_counter()
    with _tracer.start_as_current_span(
        f"job {job.queue.value}", attributes={**attributes, "motet.job.id": job.id}
    ) as span:
        outcome = _execute(conn, job, handler, stages, store, recorders)
        span.set_attribute("motet.job.outcome", outcome)
        if outcome != "completed":
            span.set_status(Status(StatusCode.ERROR, outcome))

    elapsed_ms = (time.perf_counter() - started) * 1000
    _jobs_processed.add(1, {**attributes, "motet.job.outcome": outcome})
    _job_duration.record(elapsed_ms, {**attributes, "motet.job.outcome": outcome})


def _execute(
    conn: psycopg.Connection[Any],
    job: jobs.Job,
    handler: Any,
    stages: Any,
    store: Any,
    recorders: Any,
) -> str:
    """Run one job's handler, then record the outcome in a separate transaction.

    Returns how it went, so the caller can put that on a span and a metric without the
    telemetry having to re-derive it from the job row.
    """
    context = Context(conn=conn, stages=stages, store=store)
    try:
        with conn.transaction():
            handler(context, job.payload)
    except LlmBudgetExhaustedError as exc:
        # A stage that can subdivide its work has already caught this and sent less
        # (grounding does, motet#42). Reaching here means the stage cannot, and the same
        # request would spend the same budget on every attempt — so the ladder buys five
        # identical billed failures and delays the error a user needs to see.
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("job %d on %s ran out of token budget", job.id, job.queue.value)
        with conn.transaction():
            jobs.fail(conn, job, message, max_attempts=0)
            _record_failure(conn, job, recorders, message)
        return "failed_permanently"
    except PermanentFailure as exc:
        # Retrying cannot help, so skip the backoff ladder entirely and surface it now.
        message = f"{type(exc).__name__}: {exc}"
        # Logged at ERROR *with* the exception, which is what puts it in GlitchTip: the
        # error reporter is wired through the logging integration precisely so the queue
        # runner never has to import a vendor SDK. Without this line the one failure that
        # will never be retried was also the one that reported nothing.
        logger.exception("job %d on %s failed permanently", job.id, job.queue.value)
        with conn.transaction():
            jobs.fail(conn, job, message, max_attempts=0)
            _record_failure(conn, job, recorders, message)
        return "failed_permanently"
    except Exception as exc:  # noqa: BLE001 — the queue's whole job is to survive these
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("job %d on %s raised", job.id, job.queue.value)
        with conn.transaction():
            will_retry = jobs.fail(conn, job, message)
            if not will_retry:
                _record_failure(conn, job, recorders, message)
        return "retrying" if will_retry else "failed"

    with conn.transaction():
        jobs.complete(conn, job.id)
    return "completed"


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
