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
   fail because the work rolled back, and must not roll back because recording failed —
   and the failure arm has no choice at all, because ``jobs.fail`` is written on a
   connection whose work transaction has just aborted.

Squashing these into one transaction is the obvious simplification and it is wrong: a
handler failure would roll back the attempt counter along with the work, and a poison job
would then retry forever.

**The cost of keeping 2 and 3 apart is a window, and the fence is what makes it safe.** A
worker that dies between them leaves the row ``running`` with the work durably applied, and
the lease reclaim — the recovery a killed worker depends on — hands it to somebody who
would run the whole stage again. So the work's transaction now also records *that it
committed*, in ``jobs.work_committed_attempt``: durable exactly when the work is, and read
by :func:`_execute` on the next claim, which completes such a row instead of re-running it.
That is motet#55, and the reason it is a column on the job rather than a wider state check
in the handler is that the handler cannot tell a replay from a re-script somebody asked
for — both arrive as a job against an episode in a state the stage may run from — while
the job row can, because a re-script is a different row.

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
from motet_db import llm_usage, repo
from motet_inference import Stages, get_stages
from motet_inference.llm import LlmBudgetExhaustedError
from motet_storage import ObjectStore, build_store
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from . import jobs
from .handlers import HANDLERS, Context, PermanentFailure, failure_recorders
from .llm_context import llm_job_context
from .queues import Queue

logger = logging.getLogger("motet.worker")

# Created at import against OpenTelemetry's *proxy* providers, which resolve to the real
# ones the moment `motet_obs.configure` installs them. That is what lets telemetry stay
# entirely optional: with nothing configured these are no-ops, and no code path here has
# to ask whether obs exists.
_tracer = trace.get_tracer("motet.worker")
_meter = metrics.get_meter("motet.worker")

#: The worker's numbers. Everything an operator wants to know about a queue — is it
#: draining, is it failing, is it slow — is one of these split by its attributes, which is
#: why they are a counter and a histogram rather than a gauge per queue.
#:
#: :data:`_lease_events` is the third and it is not decoration. Every outcome the keeper
#: can reach is currently a log line, and "how often does a worker lose its lease" is
#: exactly the question motet#53 exists to make answerable in Grafana. ``held`` is counted
#: as well as the three failures, because a series that only exists when something is
#: wrong cannot tell "no long jobs" from "the keeper never ran" — the
#: never-infer-"no errors"-from-"no data" trap in AGENTS.md. Cardinality is a queue times
#: four outcomes, and the volume is one point per minute per job long enough to need one.
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

_lease_events = _meter.create_counter(
    "motet.jobs.lease",
    unit="{event}",
    description="Lease-keeper outcomes for a running job, by queue and outcome.",
)

#: Rows the retention sweep deleted, by the terminal state they were in.
#:
#: Added to **even when it deletes nothing**, so the series exists on a healthy deployment.
#: Without that, "the table has nothing old in it" and "no worker has swept since the
#: window was last changed" are the same empty panel — the
#: never-infer-"no errors"-from-"no data" trap in AGENTS.md, on a stage whose whole content
#: is deletion and which therefore leaves no other trace. Cardinality is two.
_jobs_pruned = _meter.create_counter(
    "motet.jobs.pruned",
    unit="{job}",
    description="Terminal job rows deleted by the retention sweep, by state.",
)

#: ``llm_usage`` rows the same sweep deleted past ``motet_db.llm_usage.RETENTION_SECONDS``.
#: Added to at zero for :data:`_jobs_pruned`'s reason; a failed sweep is on the counter below.
_ledger_pruned = _meter.create_counter(
    "motet.llm_usage.pruned",
    unit="{row}",
    description="LLM spend-ledger rows deleted by the retention sweep.",
)

#: Ledger sweeps that failed. Its own series rather than an outcome on the one below: a
#: failure here must not relabel a jobs sweep that worked, and vice versa.
_ledger_prune_failures = _meter.create_counter(
    "motet.llm_usage.prune_failures",
    unit="{sweep}",
    description="Retention sweeps of the LLM spend ledger that failed.",
)

#: Whether the sweep ran, and whether it worked. The row count above cannot answer either.
#:
#: **A swallowed failure records no rows, which on the counter above is indistinguishable
#: from a sweep that found nothing to delete** — the same trap that counter exists to avoid,
#: one level up. The residual fault this catches is narrow but it is the shape that never
#: heals: a worker role without ``DELETE`` on ``jobs``, a lock timeout, a statement timeout.
#: Each recurs every sweep forever while the table grows, and every one of them produces a
#: perfectly quiet worker. Connectivity is *not* in that set — ``drain`` opens the same
#: connection on the same pass and does not swallow — which is why this is two series rather
#: than an alert.
_prune_sweeps = _meter.create_counter(
    "motet.jobs.prune_sweeps",
    unit="{sweep}",
    description="Retention sweeps run by a worker, by outcome.",
)

#: How much work each queue has that could start now, and how many workers could take it.
#:
#: **Gauges rather than counters, because the question is "how many workers should exist",
#: which is a level and not a rate** (motet#78). ``ready`` is the signal for a queue whose
#: rows parallelize freely; ``ready_keys`` is the signal for a serialized one, where two
#: thousand rows for one user can employ exactly one worker and depth would have a scaler
#: start a pool that spends its life deferring. See
#: :class:`~motet_workers.jobs.QueueReadiness` for what each counts.
#:
#: **Every queue is reported on every pass, not only the one being drained**, and that is
#: the difference between a signal a scaler can act on and one it cannot. A pool per queue
#: is the deployment shape this is for, and a queue scaled to zero drains nothing, emits
#: nothing, and would therefore never be scaled back up — the
#: never-infer-"no errors"-from-"no data" trap in AGENTS.md with a feedback loop attached.
#: It costs nothing to avoid: the readiness query is one grouped scan over the ready rows
#: whether it answers for one queue or for all six. The API's ``/v1/processing`` carries
#: the same numbers for the case that matters even more, which is *no worker at all*.
#:
#: **These series are sampled, not continuous, and a consumer has to know it.** The SDK's
#: last-value aggregation hands its value to the exporter and clears it, so a collection
#: interval in which no drain pass ran exports no point at all — for a one-shot Cloud Run
#: job, that is one point per execution and nothing in between. A panel or a scaler reads
#: them with ``last_over_time`` rather than expecting a value every scrape, and the gap is
#: the honest answer anyway: it is what "no worker ran" looks like.
_queue_ready = _meter.create_gauge(
    "motet.jobs.ready",
    unit="{job}",
    description="Jobs on a queue that are ready and due, by queue.",
)
_queue_ready_keys = _meter.create_gauge(
    "motet.jobs.ready_keys",
    unit="{key}",
    description="Distinct units of ready work on a queue that could run concurrently.",
)
_queue_blocked_keys = _meter.create_gauge(
    "motet.jobs.ready_keys_blocked",
    unit="{key}",
    description="Ready serialization keys already held by somebody, by queue.",
)

#: A safety stop on one invocation, so a runaway producer cannot keep a Cloud Run job
#: alive indefinitely. Reaching it is not an error — the next scheduled run continues.
MAX_JOBS_PER_RUN = 500

#: How long to wait for the lease keeper's thread after a job finishes. Short: it is a
#: daemon holding at most one connection, and a worker that blocked here would be delayed
#: by the very bookkeeping meant to keep it running.
_LEASE_JOIN_SECONDS = 5.0

#: How long the keeper waits for a connection before giving up on this touch.
#:
#: libpq's default is the operating system's TCP timeout, which is minutes — long enough
#: that a keeper stalled on an unreachable Postgres would outlive
#: :data:`_LEASE_JOIN_SECONDS`, be abandoned, and leave a thread and a socket behind on
#: *every* job for as long as the stall lasted. The always-on runner drains up to
#: :data:`MAX_JOBS_PER_RUN` jobs a pass, forever, so nothing would bound the count. Failing
#: fast is free here: the next touch is a minute away and the lease is thirty wide.
_LEASE_CONNECT_TIMEOUT_SECONDS = 10


def drain(
    queue: Queue,
    database_url: str,
    *,
    max_jobs: int = MAX_JOBS_PER_RUN,
    stages: Stages | None = None,
    store: ObjectStore | None = None,
    enrich_client: object | None = None,
) -> int:
    """Claim and run every ready job on ``queue``. Returns the number processed.

    **A long-lived worker passes ``stages`` and ``store`` in, and that is not an
    optimisation.** Resolving them here is right for a Cloud Run job, which drains once
    and exits — but ``runner all --poll-seconds N`` calls this several times a second, and
    ``real_stages()`` mints a fresh ``LlmClient`` every time it is called. OpenRouter's
    sticky upstream routing is *per client*, and that routing is what keeps the dedup
    prompt cache warm — the largest LLM cost lever in the system (see AGENTS.md). A client
    per pass would throw the cache away on every sweep and leak a connection pool doing it.

    ``enrich_client`` is the same bargain one seam along (motet#102): built once per process
    so that a deployment with no enrichment service, or an image missing ``google-auth``,
    says so at startup rather than inside the first agent run — and ``None`` means the
    handler resolves its own, which is what a one-shot drain and every test get.

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
        _record_readiness(conn)
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

            after_commit: list[Any] = []
            try:
                _run_one(
                    conn,
                    database_url,
                    job,
                    handler,
                    stages,
                    store,
                    recorders,
                    after_commit,
                    enrich_client,
                )
            finally:
                if job.serialize_key is not None:
                    jobs.unlock(conn, job.serialize_key)
            # After the unlock, so a slow step — a Gmail call, motet#96 — never holds up this
            # user's other serialized jobs, and outside the job's span and duration metric.
            _run_after_commit(after_commit, job)
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


def _record_readiness(conn: psycopg.Connection[Any]) -> None:
    """Put every queue's scaling signal on the gauges, once per :func:`drain` call.

    Beside the heartbeat and for the same reason: this is the pass saying what it sees,
    and a number nobody emits is a number nobody can scale on.

    **Once per call, where the heartbeat is once per claim, and the asymmetry is the
    cost.** The heartbeat is inside the loop because a worker chewing through
    :data:`MAX_JOBS_PER_RUN` jobs would otherwise go quiet for as long as that takes, and
    it pays one single-row upsert for it. This is an aggregate over every due row, and the
    thing reading it is a scaler that decides on a scale of tens of seconds — so sampling
    it once per pass is the rate that matters, and once per *job* would be five hundred
    times the query for no decision anybody makes differently. The cost of that choice is
    real and bounded: a pass that runs the full five hundred jobs reports the queue as it
    was when the pass started.

    Swallowed, because a worker that refused to drain because it could not *measure* the
    queue would be a much worse defect than a gap in a gauge — and swallowed is not silent,
    it is a WARNING with the stack trace. Deliberately no counter beside it, unlike the
    retention sweep: that runs on its own connection and could fail alone forever, while
    this shares the connection the claim is about to use, so a failure here is a failure
    the drain is about to report anyway.
    """
    try:
        readiness = jobs.queue_readiness(conn)
    except Exception:  # noqa: BLE001 — measuring the queue must not stop draining it
        logger.warning(
            "could not read queue readiness; the scaling gauges skip this pass",
            exc_info=True,
        )
        return
    for entry in readiness:
        attributes = {"motet.queue": entry.queue}
        _queue_ready.set(entry.ready, attributes)
        _queue_ready_keys.set(entry.ready_keys, attributes)
        _queue_blocked_keys.set(entry.blocked_keys, attributes)


def prune_jobs(database_url: str) -> jobs.Pruned:
    """Run one retention sweep over the ``jobs`` table and the ``llm_usage`` ledger.

    The ledger rides this sweep rather than having one of its own because it is the same
    shape — bounded, oldest-first, on an autocommit connection — and a second schedule
    would be a second thing that can quietly stop running (motet#92).

    The observable half of :func:`~motet_workers.jobs.prune`: that function is the SQL and
    its bounds, this is the connection, the counter and the line an operator reads. Same
    split as everything else here — ``jobs`` takes connections and never opens one.

    **Autocommit, because the batching is the bound.** Each ``DELETE`` is its own
    transaction, so the sweep holds at most one batch of row locks at a time rather than
    every lock it has taken since it started. A connection per sweep rather than one held
    for the process, for :func:`drain`'s reason: a poll loop that opens and closes heals
    itself when Postgres drops one.

    Failure is swallowed, and that is deliberate. Pruning is bookkeeping nobody asked for,
    it runs beside work somebody *is* waiting on, and the only cost of skipping an hour is
    an hour of rows. A worker that died because its retention sweep could not reach the
    database would be a defect traded for a much worse one. **Swallowed is not silent**: it
    is an ERROR with a stack trace and a point on :data:`_prune_sweeps`, because a fault that
    recurs every sweep forever is the one thing a row count cannot report.
    """
    try:
        with repo.connect(database_url) as conn:
            conn.autocommit = True
            pruned = jobs.prune(conn)
            ledger = _prune_ledger(conn)
    except Exception:  # noqa: BLE001 — a sweep must never be able to stop a worker
        # At ERROR, and on its own counter, because a swallowed failure records no rows and
        # is therefore invisible on `_jobs_pruned` — identical to a sweep that found nothing.
        # ERROR is also what reaches GlitchTip: nobody *chose* this, unlike the drain
        # trigger's expected 403, so it is a fault rather than a configuration answer.
        _prune_sweeps.add(1, {"motet.prune.outcome": "failed"})
        logger.exception("could not prune terminal job rows; will try again")
        return jobs.Pruned(deleted={}, capped=False)

    _prune_sweeps.add(1, {"motet.prune.outcome": "ok"})
    for state, count in pruned.deleted.items():
        _jobs_pruned.add(count, {"motet.job.state": state})
    logger.info(
        "pruned %d terminal job row(s): %s",
        pruned.total,
        ", ".join(f"{count} {state}" for state, count in sorted(pruned.deleted.items())),
    )
    if ledger is not None:
        _ledger_pruned.add(ledger.deleted)
        logger.info("pruned %d llm_usage row(s)", ledger.deleted)
    if pruned.capped or (ledger is not None and ledger.capped):
        # Not an error — the next sweep continues, and a backlog built up before this
        # existed drains over a few of them. Said out loud because a cap reached every
        # hour forever is the table growing faster than this removes it, and the counter
        # above cannot distinguish that from a busy deployment.
        logger.warning(
            "the retention sweep used its whole batch budget; more job or ledger rows are "
            "probably past their window and the next sweep will take them"
        )
    return pruned


def _prune_ledger(conn: psycopg.Connection[Any]) -> llm_usage.Pruned | None:
    """The ledger's half of the sweep, failing on its own so the jobs half still counts."""
    try:
        return llm_usage.prune(conn)
    except Exception:  # noqa: BLE001 — same reasoning as the jobs sweep above
        _ledger_prune_failures.add(1)
        logger.exception("could not prune llm_usage rows; will try again")
        return None


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
    attributes = {"motet.queue": job.queue.value}
    stop = threading.Event()

    def keep() -> None:
        # `wait` returns True only when the handler has finished, so a job shorter than one
        # interval — which is nearly all of them — touches nothing and costs one Event.
        while not stop.wait(interval):
            if time.monotonic() >= deadline:
                _lease_events.add(1, {**attributes, "motet.lease.outcome": "cap_reached"})
                logger.error(
                    "job %d on %s has held its lease for %ds without finishing; no longer "
                    "extending it, so another worker may reclaim it",
                    job.id,
                    job.queue.value,
                    jobs.MAX_LEASE_EXTENSION_SECONDS,
                )
                return
            try:
                with repo.connect(
                    database_url, connect_timeout=_LEASE_CONNECT_TIMEOUT_SECONDS
                ) as touch_conn:
                    touch_conn.autocommit = True
                    result = jobs.touch(touch_conn, job.id, attempts=job.attempts)
            except Exception:  # noqa: BLE001 — a keeper that dies quietly is the bug
                # Not fatal and not the end of the keeper: `STALE_LEASE_SECONDS` is many
                # intervals wide precisely so a blip does not cost a job. `continue`
                # rather than `return` is the decision — one unreachable minute must not
                # hand a healthy job to another worker.
                _lease_events.add(1, {**attributes, "motet.lease.outcome": "touch_failed"})
                logger.warning(
                    "job %d on %s: could not extend the lease, will try again in %ss",
                    job.id,
                    job.queue.value,
                    interval,
                    exc_info=True,
                )
                continue

            _lease_events.add(1, {**attributes, "motet.lease.outcome": result.value})
            if result is jobs.LeaseTouch.HELD:
                continue
            if result is jobs.LeaseTouch.SETTLED or stop.is_set():
                # The job finished while this touch was in flight. Ordinary, and the whole
                # reason `touch` classifies a miss rather than assuming the worst.
                return
            logger.error(
                "job %d on %s is no longer ours — another worker has claimed it while "
                "this one is still running it, so the stage is running twice",
                job.id,
                job.queue.value,
            )
            return

    thread = threading.Thread(target=keep, name=f"lease-{job.id}", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        # Thread or fd exhaustion. Running the job without a heartbeat is exactly the
        # behaviour this replaced, and it was survivable; killing the worker outright —
        # which is what letting this escape a context manager entered before `yield` would
        # do — is not.
        _lease_events.add(1, {**attributes, "motet.lease.outcome": "no_keeper"})
        logger.exception("job %d on %s: could not start a lease keeper", job.id, job.queue.value)
        yield
        return
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
    after_commit: list[Any] | None = None,
    enrich_client: object | None = None,
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
        # `settings` overrides in, one `llm_usage` row per completion out. Around
        # `_execute` rather than inside it so the rows are written after its transactions
        # settle — a completion billed inside a job that rolled back still happened.
        llm_job_context(conn, job),
    ):
        outcome = _execute(
            conn, database_url, job, handler, stages, store, recorders, after_commit, enrich_client
        )
        span.set_attribute("motet.job.outcome", outcome)
        # `already_applied` is a success: the row was recovered and settled, and nothing
        # about *this* job went wrong. That a worker died is carried by the WARNING and by
        # the counter's own attribute, where it does not inflate a trace error rate.
        if outcome not in ("completed", "already_applied"):
            span.set_status(Status(StatusCode.ERROR, outcome))

    elapsed_ms = (time.perf_counter() - started) * 1000
    _jobs_processed.add(1, {**attributes, "motet.job.outcome": outcome})
    _job_duration.record(elapsed_ms, {**attributes, "motet.job.outcome": outcome})


def _execute(
    conn: psycopg.Connection[Any],
    database_url: str,
    job: jobs.Job,
    handler: Any,
    stages: Any,
    store: Any,
    recorders: Any,
    after_commit: list[Any] | None = None,
    enrich_client: object | None = None,
) -> str:
    """Run one job's handler, then record the outcome in a separate transaction.

    Returns how it went, so the caller can put that on a span and a metric without the
    telemetry having to re-derive it from the job row. ``already_applied`` is one of those
    outcomes rather than a silent early return: it is how often a worker died with its work
    committed and unacknowledged, which is a number nobody could have asked for before.

    The fence is checked here rather than in :func:`~motet_workers.jobs.claim`, because a
    row whose work already landed still needs *completing* — leaving it in the queue for
    the claim to keep skipping would strand it in ``running`` forever, which is the failure
    the lease reclaim exists to prevent.

    What the handler appended to ``Context.after_commit`` is handed back on ``after_commit``
    for the caller to run once it has released the job's serialization lock — which is why
    it is an out-parameter rather than run here. A caller that passes none gets the steps
    run here instead, after the completion commits.
    """
    if job.work_committed_attempt is not None:
        # A replay, and the one thing a handler cannot recognise from where it stands. This
        # row's work is committed — `mark_work_committed` is written inside the transaction
        # that wrote it — and the worker that did it died before it could say so, so the
        # lease expired and this claim is the recovery. Recovering the *row* is right;
        # re-running the stage is not, and for `script` it is another billed completion
        # and a `failed` episode quietly put back into `rendering` with the reason it
        # failed overwritten (motet#55).
        #
        # At WARNING because it is not routine: it means a worker died mid-job, which is
        # worth seeing even though nothing was lost. The counter is what makes "how often"
        # answerable — a log line answers "which one".
        logger.warning(
            "job %d on %s was already applied on attempt %d and never marked done; "
            "completing it without running the stage again",
            job.id,
            job.queue.value,
            job.work_committed_attempt,
        )
        with conn.transaction():
            jobs.complete(conn, job.id)
        return "already_applied"

    context = Context(
        conn=conn,
        stages=stages,
        store=store,
        enrich_client=enrich_client,
        database_url=database_url,
    )
    try:
        with conn.transaction():
            handler(context, job.payload)
            # Inside the handler's transaction, which is the entire point: this is durable
            # exactly when the work is. Last, so that it is the final statement before the
            # commit and holds its row lock — which the lease keeper also wants — for as
            # little of a stage that may run for forty minutes as possible.
            jobs.mark_work_committed(conn, job.id, attempts=job.attempts)
    except LlmBudgetExhaustedError as exc:
        # A stage that can subdivide its work would catch this itself and send less
        # (motet#42). Reaching here means the stage cannot, and the same request would
        # spend the same budget on every attempt — so the ladder buys five identical
        # billed failures and delays the error a user needs to see.
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("job %d on %s ran out of token budget", job.id, job.queue.value)
        with conn.transaction():
            # Domain row first, then the job row. Both this and the handler's transaction
            # touch the two, and taking them in different orders is a deadlock between two
            # workers on one job — see `jobs.mark_work_committed`.
            _record_failure(conn, job, recorders, message)
            jobs.fail(conn, job, message, max_attempts=0)
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
            # Domain row first, then the job row. Both this and the handler's transaction
            # touch the two, and taking them in different orders is a deadlock between two
            # workers on one job — see `jobs.mark_work_committed`.
            _record_failure(conn, job, recorders, message)
            jobs.fail(conn, job, message, max_attempts=0)
        return "failed_permanently"
    except Exception as exc:  # noqa: BLE001 — the queue's whole job is to survive these
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("job %d on %s raised", job.id, job.queue.value)
        with conn.transaction():
            # Same order, and the reason the ceiling is asked about before `fail` applies
            # it rather than being read off its return value.
            retrying = jobs.will_retry(job)
            if not retrying:
                _record_failure(conn, job, recorders, message)
            jobs.fail(conn, job, message)
        return "retrying" if retrying else "failed"

    with conn.transaction():
        jobs.complete(conn, job.id)
    if after_commit is None:
        _run_after_commit(context.after_commit, job)
    else:
        after_commit.extend(context.after_commit)
    return "completed"


def _run_after_commit(steps: list[Any], job: jobs.Job) -> None:
    """Run what a handler asked to happen once its work had landed — motet#96.

    **After ``jobs.complete``, not between the two commits.** The window between the work
    commit and the completion is the one the work fence exists to cover, and a step here
    can make a network call; putting it inside that window would widen the gap in which a
    dead worker leaves a finished job ``running``. :func:`drain` runs it after releasing the
    job's serialization lock too, so a slow step does not hold up that user's next job.

    Swallowed, one step at a time: a step exists precisely because it must not be able to
    fail the job, and one step raising must not skip the next. Each step is expected to
    record its own outcome; this is the backstop for one that did not.
    """
    for step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 — the job has already succeeded; keep it that way
            logger.exception(
                "an after-commit step for job %d on %s raised", job.id, job.queue.value
            )


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
