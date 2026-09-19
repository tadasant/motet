"""Where a mailbox sync has got to, as one reading both clients render.

Two records, and neither alone can answer. ``sync_state.sync_run``, which the poll handler
adds to on every link of its chain (:func:`motet_workers.ingest.advance_sync_run`), says
what the search has *found*. The job queue says what is still *happening* — a poll
waiting or running, extract jobs still open. A sync is "done" only when both agree: the
search is exhausted and nothing it queued is still in flight.

Pure, over values, so every stage is a unit test with no database. The one judgement in
it is :data:`WORKER_FRESH`, and it is the Processing panel's own five minutes: a queued
sync with no worker alive is a sync nothing will run, and saying "queued" alone over it is
the never-infer-"no errors"-from-"no data" trap wearing a progress bar (motet#38).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from motet_db.phase2 import SourceSyncJobs

from .schemas import SourceSyncProgress

#: A heartbeat older than this is a worker that is not running. The SPA's ``WORKER_FRESH_MS``.
WORKER_FRESH = timedelta(minutes=5)

#: How long a finished or failed sync stays on the screen. Long enough that someone who
#: pressed "Sync now" and wandered off comes back to "done, 480 found" rather than to no
#: answer; short enough that Tuesday's screen does not report Monday's sync.
SETTLED_VISIBLE = timedelta(hours=1)

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
) -> SourceSyncProgress | None:
    """The stage and the counts, or ``None`` when there is no sync to report."""
    raw = sync_state.get("sync_run")
    run: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    status = run.get("status")
    started_at = _time(run.get("started_at"))
    updated_at = _time(run.get("updated_at")) or started_at

    # A run already under way is continued by a queued poll — the next link of its chain —
    # and so is still "listing". Any other open poll starts a new sync, so the old run's
    # numbers are not this one's and are not shown.
    continuing = status == "listing"
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
            waiting_on_worker=stage != "connecting" and not _worker_alive(worker_last_seen_at, now),
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
        stage = "listing" if jobs.poll_state is not None else "failed"
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
        waiting_on_worker=in_flight
        and not someone_on_it
        and not _worker_alive(worker_last_seen_at, now),
    )


def _worker_alive(seen: datetime | None, now: datetime) -> bool:
    return seen is not None and now - seen <= WORKER_FRESH


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
