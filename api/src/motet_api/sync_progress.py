"""Where a mailbox sync has got to, as one reading both clients render.

Two records, and neither alone can answer. ``sync_state.sync_run``, which the poll handler
adds to on every link of its chain (:func:`motet_workers.ingest.advance_sync_run`), says
what the search has *found*. The job queue says what is still *happening* — a poll
waiting or running, extract jobs still open. A sync is "done" only when both agree: the
search is exhausted and nothing it queued is still in flight.

Pure, over values, so every stage is a unit test with no database. Two judgements are in
it, and they are the same trap read from opposite ends. :data:`WORKER_FRESH` is the
Processing panel's own five minutes: a queued sync with no worker alive is a sync nothing
will run, and saying "queued" alone over it is the never-infer-"no errors"-from-"no data"
trap wearing a progress bar (motet#38). :data:`WORKER_STARTING` is its mirror, and it was
motet#136's residue: where the API starts the worker itself (motet#71), *no* heartbeat is
fresh at the moment somebody presses "Sync now", so reading the heartbeat alone reports
"nothing will move until a worker runs" over a container that is booting. Inferring
"nothing is coming" from "nothing has run" is the same mistake as inferring "no errors"
from "no data".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from motet_db.phase2 import SourceSyncJobs

from .schemas import SourceSyncProgress

#: A heartbeat older than this is a worker that is not running. The SPA's ``WORKER_FRESH_MS``.
WORKER_FRESH = timedelta(minutes=5)

#: How long after a poll was enqueued a worker may still be *starting* for it.
#:
#: Where ``MOTET_DRAIN_TRIGGER`` is on, the API asks Cloud Run to run the worker job in the
#: same request that enqueued the poll (motet#71) — but Cloud Run's job scheduling latency,
#: the gap between an execution being created and its container starting, was measured at
#: 90-165 seconds. An environment whose worker is one-shot and started on demand therefore
#: has *no* heartbeat inside ``WORKER_FRESH`` at the moment anybody presses "Sync now": the
#: last worker exited when it finished, and the next one has not booted yet. Read by the
#: heartbeat alone, every such sync opens by telling its owner that nothing will move until
#: a worker runs — while the worker it is waiting for is two minutes from starting. That is
#: production, and it is what "stuck on Syncing…" looked like.
#:
#: Comfortably past the 165-second ceiling so a slow start is not called a stall, and below
#: :data:`WORKER_FRESH` so that a nudge which never produced a container surrenders to the
#: heartbeat's reading rather than claiming a worker is coming forever.
WORKER_STARTING = timedelta(minutes=4)

#: How long a finished or failed sync stays on the screen. Long enough that someone who
#: pressed "Sync now" and wandered off comes back to "done, 480 found" rather than to no
#: answer; short enough that Tuesday's screen does not report Monday's sync.
SETTLED_VISIBLE = timedelta(hours=1)

#: How long a run still marked ``listing`` may have no open poll before it is called stopped.
#: The run and the job rows are read in two statements, so a final link that commits between
#: them leaves a snapshot showing the old ``listing`` run and no poll — a sync finishing, not
#: one that broke. A chain that really lost its next link is still reported, a little later.
BROKEN_CHAIN_GRACE = timedelta(minutes=2)

Stage = Literal["queued", "retrying", "connecting", "listing", "fetching", "done", "failed"]


def run_started_at(sync_state: Mapping[str, Any]) -> datetime | None:
    """The start of the sync the run record describes, or ``None`` if there is none."""
    run = sync_state.get("sync_run")
    return _time(run.get("started_at")) if isinstance(run, dict) else None


def sync_progress(
    sync_state: Mapping[str, Any],
    jobs: SourceSyncJobs,
    *,
    now: datetime,
    worker_last_seen_at: datetime | None,
    drain_trigger_enabled: bool = False,
) -> SourceSyncProgress | None:
    """The stage and the counts, or ``None`` when there is no sync to report.

    ``drain_trigger_enabled`` is whether this deployment starts a worker when it enqueues
    (``MOTET_DRAIN_TRIGGER``, :mod:`motet_api.drain`). It is the half of "a worker is
    starting" that no row can carry: the nudge is fire-and-forget and leaves no record, so
    what is inferred is that *this API asked for a worker when it wrote this poll job*.
    Deliberately conservative in both directions — it never fires where the switch is off,
    and :data:`WORKER_STARTING` expires it, so a nudge Cloud Run refused reverts to the
    heartbeat's own reading within four minutes rather than promising a worker forever.
    """
    raw = sync_state.get("sync_run")
    run: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    status = run.get("status")
    started_at = _time(run.get("started_at"))
    updated_at = _time(run.get("updated_at")) or started_at

    # A run already under way is continued by a queued poll — the next link of its chain —
    # and so is still "listing". Any other open poll starts a new sync, so the old run's
    # numbers are not this one's and are not shown.
    continuing = status == "listing"
    starting = _worker_starting(jobs, now=now, enabled=drain_trigger_enabled)
    alive = _worker_alive(worker_last_seen_at, now)
    stage: Stage
    if jobs.poll_state is not None and not continuing:
        if jobs.poll_state == "running":
            stage = "connecting"
        elif jobs.poll_attempts > 0 and jobs.poll_error:
            stage = "retrying"
        else:
            stage = "queued"
        return SourceSyncProgress(
            stage=stage,
            started_at=None,
            listed=0,
            found=0,
            found_is_lower_bound=True,
            pulled_in=0,
            remaining=0,
            failed=0,
            pages=0,
            error=jobs.poll_error if stage == "retrying" else None,
            waiting_on_worker=stage != "connecting" and not alive and not starting,
            worker_starting=stage != "connecting" and not alive and starting,
        )
    if not run or started_at is None:
        return None

    found = _count(run.get("queued"))
    open_ = jobs.extract_open
    failed = jobs.extract_failed
    pulled_in = max(0, found - open_ - failed)

    if status == "failed":
        stage = "failed"
    elif continuing:
        # Mid-chain with no poll open means the chain broke without a failure being
        # recorded — the source was paused, or its job was lost. Reported as stopped rather
        # than as a listing that will never list again.
        recent = updated_at is not None and now - updated_at <= BROKEN_CHAIN_GRACE
        stage = "listing" if jobs.poll_state is not None or recent else "failed"
    elif open_ > 0:
        stage = "fetching"
    else:
        stage = "done"

    if stage in ("done", "failed") and (updated_at is None or now - updated_at > SETTLED_VISIBLE):
        return None

    error: str | None = None
    if stage == "failed":
        recorded = run.get("error")
        error = (
            recorded
            if isinstance(recorded, str) and recorded
            else "The sync stopped before it finished listing. Sync now resumes it."
        )
    elif stage == "listing" and jobs.poll_attempts > 0 and jobs.poll_error:
        error = jobs.poll_error

    in_flight = stage in ("listing", "fetching")
    someone_on_it = jobs.poll_state == "running" or jobs.extract_running > 0
    unattended = in_flight and not someone_on_it and not alive
    return SourceSyncProgress(
        stage=stage,
        started_at=started_at,
        listed=_count(run.get("listed")),
        found=found,
        found_is_lower_bound=stage == "listing",
        pulled_in=pulled_in,
        remaining=open_,
        failed=failed,
        pages=_count(run.get("pages")),
        error=error,
        waiting_on_worker=unattended and not starting,
        worker_starting=unattended and starting,
    )


def _worker_alive(seen: datetime | None, now: datetime) -> bool:
    return seen is not None and now - seen <= WORKER_FRESH


def _worker_starting(jobs: SourceSyncJobs, *, now: datetime, enabled: bool) -> bool:
    """Whether a worker was asked for, recently enough that it may still be booting.

    Read off the **poll** job alone, because that is the one this API enqueues and nudges
    for. An extract job or a further link of a poll chain is written by the worker itself,
    which fires no trigger (:mod:`motet_api.drain` says why the nudge lives in the request
    path and not in the ``enqueue_*`` helpers) — so a worker-written job must not be read as
    an execution starting. What keeps that true is *not* this function: it is that a worker
    which just wrote one is alive, and every caller gates on the heartbeat being stale.

    Two things carry that gate, and both are load-bearing rather than incidental. ``drain``
    writes the heartbeat before each claim and ``Queue.POLL`` is first in
    ``queues.PIPELINE``, so the six later drains of one pass each refresh it; and
    :data:`WORKER_STARTING` sits a minute inside :data:`WORKER_FRESH`. A worker killed
    immediately after committing a chain link therefore leaves a heartbeat older than the
    job by the handler's own duration, and only a handler slower than that minute could
    open a window where this reads a worker-written job as a starting one. Moving the
    heartbeat, reordering ``PIPELINE``, or closing that minute would each make it reachable.
    """
    enqueued = jobs.poll_enqueued_at
    return enabled and enqueued is not None and timedelta() <= now - enqueued <= WORKER_STARTING


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
