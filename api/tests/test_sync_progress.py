"""Where a mailbox sync has got to: the pure stage function, and the route that reports it.

The first half is ``motet_api.sync_progress`` over values, one test per stage and per edge a
screen would otherwise lie about. The second half drives a real poll chain and real
extraction through ``GET /v1/sources``, because "the run's start is the clock its jobs are
stamped with" and "the open count comes off migration 0008's index" are claims about
Postgres rather than about a function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.sync_progress import (
    BROKEN_CHAIN_GRACE,
    SETTLED_VISIBLE,
    WORKER_FRESH,
    WORKER_STARTING,
    sync_progress,
)
from motet_db import repo
from motet_db.phase2 import SourceSyncJobs
from motet_sources import FakeMailClient, RawMessage
from motet_workers import Queue, drain

NOW = datetime(2026, 9, 19, 16, 0, tzinfo=UTC)
ALIVE = NOW - timedelta(seconds=20)
GONE = NOW - WORKER_FRESH - timedelta(seconds=1)
NO_JOBS = SourceSyncJobs()


def run(
    status: str, *, queued: int = 0, listed: int = 0, ago: timedelta = timedelta(0), **extra: Any
) -> dict[str, Any]:
    at = (NOW - ago).isoformat()
    return {
        "sync_run": {
            "started_at": at,
            "updated_at": at,
            "listed": listed,
            "queued": queued,
            "pages": 3,
            "status": status,
            "error": None,
            **extra,
        }
    }


def progress(
    state: dict[str, Any],
    jobs: SourceSyncJobs = NO_JOBS,
    seen: datetime | None = ALIVE,
    *,
    trigger: bool = False,
) -> Any:
    return sync_progress(
        state, jobs, now=NOW, worker_last_seen_at=seen, drain_trigger_enabled=trigger
    )


# --- before anything is found ---------------------------------------------------------


def test_no_run_and_no_poll_is_nothing_to_report() -> None:
    assert progress({}) is None


def test_a_waiting_poll_is_queued_and_says_whether_a_worker_will_take_it() -> None:
    queued = SourceSyncJobs(poll_state="ready")
    assert progress({}, queued).stage == "queued"
    assert progress({}, queued).waiting_on_worker is False
    # Tadas's production sync: the poll sat there, and no worker exists to run it.
    stuck = progress({}, queued, seen=None)
    assert (stuck.stage, stuck.waiting_on_worker) == ("queued", True)
    assert progress({}, queued, seen=GONE).waiting_on_worker is True


def test_a_worker_being_started_is_not_a_worker_that_will_never_come() -> None:
    """Tadas's "stuck on Syncing…", and the half of it that outlived the infrastructure fix.

    Where the API starts the worker itself (motet#71) the worker is one-shot, so at the
    moment "Sync now" is pressed the newest heartbeat is always older than ``WORKER_FRESH``
    — the last worker exited when it finished. Read by the heartbeat alone, every sync in
    such a deployment opens by announcing that nothing will move, for the minute or two
    Cloud Run takes to start the container the same request asked for.
    """
    fresh = SourceSyncJobs(poll_state="ready", poll_enqueued_at=NOW - timedelta(seconds=30))
    shown = progress({}, fresh, seen=GONE, trigger=True)
    assert (shown.stage, shown.worker_starting, shown.waiting_on_worker) == ("queued", True, False)


def test_a_worker_that_was_never_asked_for_is_still_reported_as_missing() -> None:
    """The switch is off — no execution was started — so the honest answer is the old one."""
    fresh = SourceSyncJobs(poll_state="ready", poll_enqueued_at=NOW - timedelta(seconds=30))
    shown = progress({}, fresh, seen=GONE, trigger=False)
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, True)


def test_a_nudge_that_produced_no_container_stops_claiming_one_is_coming() -> None:
    """A refused or lost nudge must not promise a worker forever: the window expires and
    the heartbeat's own reading takes over."""
    stale = SourceSyncJobs(
        poll_state="ready", poll_enqueued_at=NOW - WORKER_STARTING - timedelta(seconds=1)
    )
    shown = progress({}, stale, seen=GONE, trigger=True)
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, True)


def test_a_live_worker_is_never_reported_as_merely_starting() -> None:
    """A poll job written by the worker mid-chain fires no nudge, and the worker that wrote
    it is alive — so the heartbeat, not the job's age, is what answers."""
    fresh = SourceSyncJobs(poll_state="ready", poll_enqueued_at=NOW - timedelta(seconds=5))
    shown = progress({}, fresh, seen=ALIVE, trigger=True)
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, False)


def test_a_poll_job_stamped_in_the_future_is_not_a_worker_starting() -> None:
    """Clock skew between the API and Postgres must not open the window indefinitely."""
    ahead = SourceSyncJobs(poll_state="ready", poll_enqueued_at=NOW + timedelta(minutes=10))
    assert progress({}, ahead, seen=GONE, trigger=True).worker_starting is False


def test_extraction_left_unattended_can_also_be_waiting_on_a_starting_worker() -> None:
    """The stage after the search, with the poll chain's own job still open behind it."""
    jobs = SourceSyncJobs(
        poll_state="ready",
        poll_enqueued_at=NOW - timedelta(seconds=20),
        extract_open=10,
    )
    shown = progress(run("listing", queued=10), jobs, seen=GONE, trigger=True)
    assert (shown.stage, shown.worker_starting, shown.waiting_on_worker) == (
        "listing",
        True,
        False,
    )


def test_a_poll_a_worker_already_holds_is_neither_waiting_nor_starting() -> None:
    """`connecting` means a worker has it: both flags are claims about one that is absent."""
    held = SourceSyncJobs(poll_state="running", poll_enqueued_at=NOW - timedelta(seconds=5))
    shown = progress({}, held, seen=GONE, trigger=True)
    assert (shown.stage, shown.waiting_on_worker, shown.worker_starting) == (
        "connecting",
        False,
        False,
    )


@pytest.mark.parametrize("trigger", [True, False])
@pytest.mark.parametrize("seen", [ALIVE, GONE, None])
@pytest.mark.parametrize("poll_state", [None, "ready", "running"])
@pytest.mark.parametrize("age", [timedelta(seconds=5), WORKER_STARTING + timedelta(seconds=1)])
def test_the_two_readings_are_never_both_true(
    trigger: bool, seen: datetime | None, poll_state: str | None, age: timedelta
) -> None:
    """They are one question with two answers, and both clients branch on them in order.

    A state reporting both would show the stalled sentence over a booting worker, which is
    the defect this field exists to remove — so it is asserted over the whole cross product
    rather than in the branches that happened to get a test.
    """
    jobs = SourceSyncJobs(poll_state=poll_state, poll_enqueued_at=NOW - age)
    for state in ({}, run("listing", queued=60), run("listed", queued=10)):
        shown = progress(state, jobs, seen=seen, trigger=trigger)
        if shown is None:
            continue
        assert not (shown.waiting_on_worker and shown.worker_starting), shown.stage


def test_a_claimed_poll_with_nothing_listed_yet_is_connecting() -> None:
    shown = progress({}, SourceSyncJobs(poll_state="running"), seen=GONE)
    assert shown.stage == "connecting"
    assert shown.waiting_on_worker is False, "a running job is a worker"


def test_a_poll_backing_off_after_a_failure_is_retrying_and_says_why() -> None:
    shown = progress(
        {}, SourceSyncJobs(poll_state="ready", poll_attempts=1, poll_error="429 rate limited")
    )
    assert (shown.stage, shown.error) == ("retrying", "429 rate limited")


def test_a_new_poll_after_a_finished_sync_does_not_show_the_old_numbers() -> None:
    shown = progress(run("listed", queued=480), SourceSyncJobs(poll_state="ready"))
    assert (shown.stage, shown.found, shown.started_at) == ("queued", 0, None)


# --- while the search is listing -------------------------------------------------------


def test_a_chain_still_listing_is_a_lower_bound_with_progress_through_what_it_found() -> None:
    shown = progress(
        run("listing", queued=480, listed=600),
        SourceSyncJobs(poll_state="ready", extract_open=360, extract_running=4),
    )
    assert shown.stage == "listing"
    assert shown.found_is_lower_bound is True
    assert (shown.found, shown.pulled_in, shown.remaining, shown.listed) == (480, 120, 360, 600)


def test_the_next_link_running_is_still_listing_not_a_new_sync() -> None:
    shown = progress(run("listing", queued=60), SourceSyncJobs(poll_state="running"))
    assert (shown.stage, shown.found) == ("listing", 60)


def test_a_chain_that_lost_its_next_poll_is_reported_stopped_not_listing_forever() -> None:
    stale = BROKEN_CHAIN_GRACE + timedelta(seconds=1)
    shown = progress(run("listing", queued=60, ago=stale), SourceSyncJobs())
    assert shown.stage == "failed"
    assert shown.error is not None and "Sync now" in shown.error


def test_a_final_link_committing_between_the_two_reads_is_not_a_failure() -> None:
    """The run and the jobs are two statements: a snapshot can show the old ``listing`` run
    beside no open poll because the last link finished in between. That is a sync ending."""
    shown = progress(run("listing", queued=60, ago=timedelta(seconds=5)), SourceSyncJobs())
    assert shown.stage == "listing"


# --- after the search is exhausted ------------------------------------------------------


def test_a_finished_search_with_extraction_left_is_fetching_with_an_exact_total() -> None:
    shown = progress(run("listed", queued=480), SourceSyncJobs(extract_open=30, extract_failed=2))
    assert shown.stage == "fetching"
    assert shown.found_is_lower_bound is False
    assert (shown.found, shown.pulled_in, shown.remaining, shown.failed) == (480, 448, 30, 2)


def test_fetching_with_nobody_extracting_and_no_worker_says_so() -> None:
    shown = progress(run("listed", queued=10), SourceSyncJobs(extract_open=10), seen=GONE)
    assert shown.waiting_on_worker is True
    working = progress(
        run("listed", queued=10), SourceSyncJobs(extract_open=10, extract_running=1), seen=GONE
    )
    assert working.waiting_on_worker is False


def test_nothing_left_in_flight_is_done_and_shown_for_an_hour() -> None:
    shown = progress(run("listed", queued=480, ago=timedelta(minutes=5)))
    assert (shown.stage, shown.pulled_in, shown.remaining) == ("done", 480, 0)
    assert progress(run("listed", queued=480, ago=SETTLED_VISIBLE + timedelta(minutes=1))) is None


def test_a_sync_that_gave_up_says_why_and_stops_spinning() -> None:
    shown = progress(
        run("failed", queued=60, error="Gmail refused the credential"),
        SourceSyncJobs(extract_open=5),
    )
    assert (shown.stage, shown.error, shown.found, shown.remaining) == (
        "failed",
        "Gmail refused the credential",
        60,
        5,
    )
    assert shown.waiting_on_worker is False
    assert progress(run("failed", ago=SETTLED_VISIBLE + timedelta(minutes=1))) is None


def test_an_unreadable_run_record_is_nothing_rather_than_a_500() -> None:
    assert progress({"sync_run": {"started_at": "yesterday", "queued": "lots"}}) is None
    assert progress({"sync_run": "garbage"}) is None


# --- through the route ------------------------------------------------------------------

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REDIRECT = "https://app.example.invalid/oauth/callback"


@pytest.fixture
def api(
    db: psycopg.Connection[Any], _migrated: str, object_store: Any, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def gmail_progress(api: TestClient, source_id: str) -> dict[str, Any] | None:
    listed = api.get("/v1/sources", headers=AUTH).json()
    found = next(source for source in listed if source["id"] == source_id)
    shown: dict[str, Any] | None = found["sync_progress"]
    return shown


def test_a_sync_reports_each_stage_as_its_jobs_run(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued → listing (at least 60) → fetching (exactly 130) → done, off real job rows."""
    mailbox = FakeMailClient(
        messages=[RawMessage(id=f"m{i:03d}", raw=b"") for i in range(130)], page_size=20
    )
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    ).json()
    source_id = started["source_id"]
    api.post(
        "/v1/sources/callback",
        json={"state": started["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )

    # The callback queued the first poll and no worker has ever run.
    shown = gmail_progress(api, source_id)
    assert shown is not None
    assert (shown["stage"], shown["waiting_on_worker"]) == ("queued", True)

    # One link of the chain: a worker ran (a heartbeat), found 60 and queued the next.
    drain(Queue.POLL, _migrated, max_jobs=1)
    shown = gmail_progress(api, source_id)
    assert shown is not None
    assert (shown["stage"], shown["found"], shown["found_is_lower_bound"]) == ("listing", 60, True)
    assert (shown["pulled_in"], shown["remaining"], shown["waiting_on_worker"]) == (0, 60, False)

    drain(Queue.POLL, _migrated)
    shown = gmail_progress(api, source_id)
    assert shown is not None
    assert (shown["stage"], shown["found"], shown["found_is_lower_bound"]) == (
        "fetching",
        130,
        False,
    )
    assert shown["remaining"] == 130

    # The synthesized messages are empty, so extraction skips every one — which is still
    # progress through the work, and nothing is left in flight.
    drain(Queue.EXTRACT, _migrated)
    shown = gmail_progress(api, source_id)
    assert shown is not None
    assert (shown["stage"], shown["pulled_in"], shown["remaining"], shown["failed"]) == (
        "done",
        130,
        0,
        0,
    )


def test_sync_now_answers_with_the_sync_already_queued(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer to "Sync now" has to *be* the start of the sync, not a row with nothing on it.

    A regression guard rather than a fix: ``_source_response`` computes the progress from
    its own default argument, so no single-source route says anything about it and nothing
    in ``main.py`` would notice that default being dropped. It matters most to the iOS app,
    which *keeps* this response and watches the row it puts in place — a null progress there
    leaves the button falling straight back to "Sync now" and the detail screen polling
    nothing, where the SPA would escape it by throwing the answer away and re-fetching.
    """
    mailbox = FakeMailClient(messages=[], page_size=20)
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    ).json()
    connected = api.post(
        "/v1/sources/callback",
        json={"state": started["state"], "code": "fake-auth-code"},
        headers=AUTH,
    ).json()
    # Completing the consent queues the first poll, and says so in its own answer.
    assert connected["sync_progress"] is not None
    assert connected["sync_progress"]["stage"] == "queued"

    answered = api.post(f"/v1/sources/{started['source_id']}/poll", headers=AUTH)
    assert answered.status_code == 200
    shown = answered.json()["sync_progress"]
    assert shown is not None, "Sync now answered with no sync"
    assert shown["stage"] == "queued"
    # And it is the same reading the list gives, rather than a second opinion.
    assert shown == gmail_progress(api, started["source_id"])


def test_a_label_sync_save_does_not_wipe_a_sync_in_flight(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guard one route along: saving labels answers with a full row, which the iOS
    app puts back into its list, so a null progress here would blank a running sync's panel
    mid-sync. Three routes return a single source and all three rely on the same default."""
    mailbox = FakeMailClient(messages=[], page_size=20)
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    ).json()
    api.post(
        "/v1/sources/callback",
        json={"state": started["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    saved = api.put(
        f"/v1/sources/{started['source_id']}/label-sync",
        json={"remove_label": "Newsletters", "add_label": "Completed"},
        headers=AUTH,
    )
    assert saved.status_code == 200
    assert saved.json()["sync_progress"] is not None


class _Trigger:
    """A drain trigger that is configured. Only ``enabled`` is read on this path."""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def fire(self, reason: object) -> None:
        return None


@pytest.mark.parametrize(
    ("enabled", "expected"), [(True, {"worker_starting"}), (False, {"waiting_on_worker"})]
)
def test_the_route_reports_which_reading_this_deployment_is_entitled_to(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, enabled: bool, expected: set[str]
) -> None:
    """The one assertion that the flag reaches a client, rather than being computed.

    Every other ``worker_starting`` case drives the pure function and passes
    ``drain_trigger_enabled`` by hand, so deleting the one line in ``_sync_progress`` that
    reads the real trigger would leave the suite green and the field permanently false in
    every deployment — the computed-but-never-delivered failure this field exists to fix.
    """
    from motet_api import deps as api_deps  # noqa: PLC0415 — the module the route resolves

    monkeypatch.setattr(api_deps, "_trigger", _Trigger(enabled=enabled))
    mailbox = FakeMailClient(messages=[], page_size=20)
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    ).json()
    api.post(
        "/v1/sources/callback",
        json={"state": started["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    # No worker has ever run here, so the heartbeat is absent and the poll was just written.
    shown = gmail_progress(api, started["source_id"])
    assert shown is not None
    assert shown["stage"] == "queued"
    true_flags = {name for name in ("waiting_on_worker", "worker_starting") if shown[name]}
    assert true_flags == expected


def test_the_paste_source_reports_no_progress(api: TestClient) -> None:
    listed = api.get("/v1/sources", headers=AUTH).json()
    paste = next(source for source in listed if source["id"] == repo.PASTE_SOURCE_ID)
    assert paste["sync_progress"] is None


def test_the_poll_half_of_the_job_read_is_answered_by_the_partial_indexes(
    db: psycopg.Connection[Any], _migrated: str
) -> None:
    """Polled every two seconds during a sync: an ``IN`` list scanned the whole table."""
    db.execute("SET enable_seqscan = off")
    plan = "\n".join(
        row["QUERY PLAN"]
        for row in db.execute(
            "EXPLAIN SELECT 1 FROM jobs WHERE queue = 'poll' "
            "AND (state = 'ready' OR state = 'running') AND payload ->> 'source_id' = 'x'"
        ).fetchall()
    )
    db.execute("RESET enable_seqscan")
    assert "jobs_ready_idx" in plan and "jobs_stale_idx" in plan, plan
    import inspect  # noqa: PLC0415

    from motet_db import phase2  # noqa: PLC0415

    assert "(state = 'ready' OR state = 'running')" in inspect.getsource(phase2.source_sync_jobs)
