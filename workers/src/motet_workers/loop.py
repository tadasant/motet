"""The worker loop — claim jobs off one queue, run them, record how each went.

A worker takes a queue name and drains it. **There are two shapes and both are real
deployments**: a Cloud Run job calls this once and exits, and ``runner all --poll-seconds``
calls it in a loop forever. The job shape came first and for a long time was the only one,
which is motet#38 — a job has to be *started*, and the only thing that started one was a
human dispatching a workflow.

The connection is opened and closed **per call**, and that stays true in the poll loop even
though the stages and the object store are now hoisted out of it (see :func:`drain`). The
two are not the same trade: an ``LlmClient`` per pass throws away OpenRouter's sticky
routing and with it the dedup prompt cache, while a connection per pass costs a handshake
and buys a poll loop that heals itself when Postgres drops one — a process that exits
cannot leak a connection, a lock, or a half-finished transaction across runs, and a pass
that ends is the same guarantee at a smaller scale.

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

**A fourth thing runs beside all three: the lease keeper** (:func:`_hold_lease`). The
handler's transaction is open on the connection above for as long as the handler runs, so
nothing written on it is visible to anyone until it commits — which means the one row that
has to stay fresh while a job is slow, the job's own ``locked_at``, cannot be written from
there. The keeper is therefore a thread with a connection of its own, and it lives here
rather than in ``jobs`` because that module takes connections and never opens one.

Being a thread *in this process* is the design rather than an implementation detail: the
liveness it reports is the worker's own, so a SIGKILL, an OOM or a task timeout takes the
heartbeat with the job and the ordinary stale window still recovers the row. See motet#53.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
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

#: How long to wait for the lease keeper's thread after a job finishes. Short: it is a
#: daemon holding at most one connection, and a worker that blocked here would be delayed
#: by the very bookkeeping meant to keep it running.
_LEASE_JOIN_SECONDS = 5.0


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
        while processed < max_jobs:
            # Before every claim, including the one that finds nothing. "A worker is
            # running" is what an *empty* pass proves, and it is the fact motet#38 turned
            # on: with no heartbeat, a queue nothing is draining looks exactly like a queue
            # that is draining fine and has nothing to do.
            #
            # Inside the loop rather than only above it, because a drain runs up to
            # `MAX_JOBS_PER_RUN` jobs and a busy worker would otherwise go quiet for as
            # long as that takes — reporting "nothing is processing" over a list of items
            # it is at that moment processing. One upsert of one row per job, against a
            # job that is about to call a model.
            #
            # What this still cannot cover is a *single* job longer than the client's
            # freshness window; a large TTS render is the realistic one. The surfaces
            # that read this are built so that the residual case degrades quietly rather
            # than into a contradiction — see `web/src/screens/Processing.tsx`.
            repo.record_worker_heartbeat(conn, queue.value)
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
                _run_one(conn, database_url, job, handler, stages, store, recorders)
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


@contextlib.contextmanager
def _hold_lease(database_url: str, job: jobs.Job) -> Iterator[None]:
    """Keep saying this worker is still on ``job``, for as long as its handler runs.

    Without this, a job slower than :data:`~motet_workers.jobs.STALE_LEASE_SECONDS` became
    claimable while the worker running it was perfectly healthy, and a second worker redid
    the whole stage — motet#53. The lease reclaim is still the recovery for a worker that
    *died*; this is what stops it firing on one that has not.

    Three properties, and each is deliberate:

    * **Its own connection**, opened per touch. The caller's is inside the handler's
      transaction, where an ``UPDATE`` would be invisible until commit — which is the one
      moment it is no longer needed. Per touch rather than held for the life of the job,
      because a connection idle for forty minutes is one a proxy is entitled to drop, and
      reconnecting each minute is the cheaper way to be sure.
    * **It stops when the process does.** A daemon thread cannot outlive its worker, so the
      liveness this reports is real and a killed worker's row still goes stale.
    * **It gives up.** Past :data:`~motet_workers.jobs.MAX_LEASE_EXTENSION_SECONDS` a
      wedged-but-alive handler stops being covered, and its row falls back to the ordinary
      stale window. That is the failure this leans *toward*: a duplicated run, which costs
      money and is visible, rather than a row stranded in ``running`` forever, which costs
      an episode and is only fixable with SQL that invariant 10 forbids.

    The constants are read here rather than captured as defaults so that a test can move
    them without reaching inside this function.
    """
    interval = jobs.LEASE_TOUCH_SECONDS
    deadline = time.monotonic() + jobs.MAX_LEASE_EXTENSION_SECONDS
    stop = threading.Event()

    def keep() -> None:
        # `wait` returns True only when the handler has finished, so a job shorter than one
        # interval — which is nearly all of them — touches nothing and costs one Event.
        while not stop.wait(interval):
            if time.monotonic() >= deadline:
                logger.error(
                    "job %d on %s has held its lease for %ds without finishing; no longer "
                    "extending it, so another worker may reclaim it",
                    job.id,
                    job.queue.value,
                    jobs.MAX_LEASE_EXTENSION_SECONDS,
                )
                return
            try:
                with repo.connect(database_url) as touch_conn:
                    touch_conn.autocommit = True
                    held = jobs.touch(touch_conn, job.id, attempts=job.attempts)
            except Exception:  # noqa: BLE001 — a keeper that dies quietly is the bug
                # Not fatal and not the end of the keeper: `STALE_LEASE_SECONDS` is many
                # intervals wide precisely so a blip does not cost a job.
                logger.warning(
                    "job %d on %s: could not extend the lease, will try again in %ss",
                    job.id,
                    job.queue.value,
                    interval,
                    exc_info=True,
                )
                continue
            if not held:
                logger.error(
                    "job %d on %s is no longer ours — another worker has claimed it while "
                    "this one is still running it, so the stage is running twice",
                    job.id,
                    job.queue.value,
                )
                return

    thread = threading.Thread(target=keep, name=f"lease-{job.id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # Bounded: the thread may be mid-connect, and a worker must not be held up by its
        # own bookkeeping. It is a daemon, so anything left cannot outlive the process.
        thread.join(timeout=_LEASE_JOIN_SECONDS)


def _run_one(
    conn: psycopg.Connection[Any],
    database_url: str,
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
    with (
        _tracer.start_as_current_span(
            f"job {job.queue.value}", attributes={**attributes, "motet.job.id": job.id}
        ) as span,
        # Around `_execute` rather than around the handler, so the lease also covers
        # recording the outcome: a job whose lease lapsed between finishing and being
        # marked done is one another worker takes and runs again.
        _hold_lease(database_url, job),
    ):
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
