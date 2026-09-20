"""Where an episode is between "make it" and a file to play: the pure function, and the route.

The first half is ``motet_api.episode_progress`` over values, one test per state a screen
would otherwise describe wrongly. The second half drives a real episode through real
``assemble``/``script``/``tts`` job rows and reads it back off ``GET /v1/episodes``, because
"the step comes off the episode and the stage off the job" and "the count is the index's" are
claims about Postgres rather than about a function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.episode_progress import (
    ESTIMATE_MIN_SAMPLES,
    QUEUE_FOR_STEP,
    SETTLED_VISIBLE,
    STEP_FOR_QUEUE,
    STEP_FOR_STATE,
    STEP_ORDER,
    WORKER_FRESH,
    WORKER_STARTING,
    build_progress,
    estimate_ms,
    reports_progress,
)
from motet_db import EpisodeKind, EpisodeState
from motet_db.models import StoredClaim, StoredEpisode, StoredSegment
from motet_db.repo import EpisodeBuild, EpisodeJob
from motet_workers import DEFAULT_MAX_ATTEMPTS, Queue, drain, handlers
from motet_workers.queues import PIPELINE
from psycopg.rows import dict_row

NOW = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
ALIVE = NOW - timedelta(seconds=20)
GONE = NOW - WORKER_FRESH - timedelta(seconds=1)

#: Every pipeline queue heartbeating at once — the `runner all` shape, which is what both
#: deployed environments run. A test that cares about one queue names it instead.
EVERY_QUEUE_ALIVE = dict.fromkeys(QUEUE_FOR_STEP.values(), ALIVE)
EVERY_QUEUE_GONE = dict.fromkeys(QUEUE_FOR_STEP.values(), GONE)


def episode(
    state: EpisodeState,
    *,
    segments: int = 0,
    claims: int = 0,
    rendered: int = 0,
    created: timedelta = timedelta(seconds=30),
    published: timedelta | None = None,
    updated: timedelta | None = None,
    last_error: str | None = None,
) -> StoredEpisode:
    return StoredEpisode(
        id="ep_1",
        user_id="motet-owner",
        title="Episode",
        state=state,
        kind=EpisodeKind.MANUAL,
        rule=None,
        max_duration_ms=1_200_000,
        duration_ms=0,
        audio_key=None,
        audio_bytes=None,
        audio_media_type=None,
        last_error=last_error,
        listened_through_ms=0,
        keep_in_backlog=False,
        created_at=NOW - created,
        updated_at=NOW - (updated if updated is not None else timedelta(0)),
        published_at=None if published is None else NOW - published,
        segments=tuple(
            StoredSegment(
                id=f"seg_{i}",
                news_item_id=f"ni_{i}",
                position=i,
                text="spoken",
                start_ms=0,
                duration_ms=0,
                claims=tuple(
                    StoredClaim(
                        id=f"cl_{i}_{j}",
                        position=j,
                        text="a claim",
                        source_item_id="si_1",
                        span_start=0,
                        span_end=5,
                        start_ms=0,
                        duration_ms=0,
                    )
                    for j in range(claims)
                ),
            )
            for i in range(segments)
        ),
        rendered_segments=rendered,
    )


def job(
    queue: str,
    state: str = "ready",
    *,
    attempts: int = 0,
    error: str | None = None,
    age: timedelta = timedelta(seconds=30),
) -> EpisodeJob:
    return EpisodeJob(
        queue=queue,
        state=state,
        attempts=attempts,
        run_at=NOW + timedelta(seconds=30),
        last_error=error,
        created_at=NOW - age,
    )


def progress(
    stored: StoredEpisode,
    open_job: EpisodeJob | None,
    *,
    heartbeats: dict[str, datetime] | None = None,
    samples: tuple[int, ...] = (),
    trigger: bool = False,
) -> Any:
    """The reading, with the one-statement build record assembled from the episode.

    The API reads the episode's state beside its job in a single statement, so the two
    agree by construction; these fixtures build the pair the same way.
    """
    build = EpisodeBuild(
        state=stored.state.value,
        published_at=stored.published_at,
        updated_at=stored.updated_at,
        job=open_job,
    )
    return build_progress(
        stored,
        build,
        now=NOW,
        heartbeats=EVERY_QUEUE_ALIVE if heartbeats is None else heartbeats,
        samples=samples,
        drain_trigger_enabled=trigger,
    )


# --- the steps, in order --------------------------------------------------------------


def test_a_fresh_episode_is_queued_for_assembly_and_says_nothing_has_it() -> None:
    shown = progress(episode(EpisodeState.PENDING), job("assemble"))
    assert shown is not None
    assert (shown.step, shown.stage, shown.steps_done) == ("assemble", "queued", 0)
    assert (shown.news_items, shown.claims, shown.segments_rendered) == (0, 0, 0)
    assert shown.elapsed_ms == 30_000
    assert shown.waiting_on_worker is False


def test_a_queued_episode_with_no_worker_in_five_minutes_says_nothing_will_move_it() -> None:
    shown = progress(episode(EpisodeState.PENDING), job("assemble"), heartbeats=EVERY_QUEUE_GONE)
    assert shown is not None and shown.waiting_on_worker is True


def test_a_queued_episode_with_no_worker_ever_says_the_same() -> None:
    shown = progress(episode(EpisodeState.PENDING), job("assemble"), heartbeats={})
    assert shown is not None and shown.waiting_on_worker is True


def test_a_worker_that_was_just_asked_for_is_starting_not_missing() -> None:
    """Production's shape: the worker is one-shot, so its heartbeat is always stale at the
    moment an episode is created — and the container the API just asked Cloud Run for is a
    minute or two away. Read by the heartbeat alone that is "nothing will build this" over
    every build's first minute, which is what Tadas saw (motet#137's sync-panel bug, one
    surface along)."""
    shown = progress(
        episode(EpisodeState.PENDING),
        job("assemble", age=timedelta(seconds=40)),
        heartbeats=EVERY_QUEUE_GONE,
        trigger=True,
    )
    assert shown is not None
    assert (shown.worker_starting, shown.waiting_on_worker) == (True, False)


def test_a_worker_asked_for_too_long_ago_is_missing_after_all() -> None:
    """The nudge is fire-and-forget: one Cloud Run refused leaves no record, so the claim
    that a worker is coming expires with the window rather than standing forever."""
    shown = progress(
        episode(EpisodeState.PENDING),
        job("assemble", age=WORKER_STARTING + timedelta(seconds=1)),
        heartbeats=EVERY_QUEUE_GONE,
        trigger=True,
    )
    assert shown is not None
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, True)


def test_a_deployment_that_starts_no_worker_never_claims_one_is_starting() -> None:
    shown = progress(
        episode(EpisodeState.PENDING),
        job("assemble", age=timedelta(seconds=40)),
        heartbeats=EVERY_QUEUE_GONE,
        trigger=False,
    )
    assert shown is not None
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, True)


def test_only_the_assemble_job_can_mean_a_worker_is_starting() -> None:
    """The script and tts jobs are written by the worker, which fires no trigger; a young
    one of those with a stale heartbeat is a worker that died, not one that is booting."""
    shown = progress(
        episode(EpisodeState.SCRIPTING, segments=3),
        job("script", age=timedelta(seconds=40)),
        heartbeats=EVERY_QUEUE_GONE,
        trigger=True,
    )
    assert shown is not None
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, True)


def test_a_live_worker_is_neither_starting_nor_missing() -> None:
    shown = progress(
        episode(EpisodeState.PENDING), job("assemble", age=timedelta(seconds=40)), trigger=True
    )
    assert shown is not None
    assert (shown.worker_starting, shown.waiting_on_worker) == (False, False)


def test_a_claimed_assemble_job_is_running() -> None:
    shown = progress(episode(EpisodeState.PENDING), job("assemble", "running"))
    assert shown is not None
    assert (shown.step, shown.stage, shown.waiting_on_worker) == ("assemble", "running", False)


def test_scripting_reports_the_stories_assembly_chose() -> None:
    shown = progress(episode(EpisodeState.SCRIPTING, segments=8), job("script", "running"))
    assert shown is not None
    assert (shown.step, shown.stage, shown.steps_done) == ("script", "running", 1)
    assert (shown.news_items, shown.claims) == (8, 0)


def test_rendering_reports_segments_recorded_out_of_the_stories() -> None:
    stored = episode(EpisodeState.RENDERING, segments=9, claims=3, rendered=4)
    shown = progress(stored, job("tts", "running"))
    assert shown is not None
    assert (shown.step, shown.stage, shown.steps_done) == ("tts", "running", 2)
    assert (shown.news_items, shown.claims, shown.segments_rendered) == (9, 27, 4)


def test_a_render_count_is_never_more_than_the_segments_it_counts() -> None:
    """A stale tally from a longer previous render must not read as 12 of 9."""
    stored = episode(EpisodeState.RENDERING, segments=9, rendered=12)
    shown = progress(stored, job("tts", "running"))
    assert shown is not None and shown.segments_rendered == 9


def test_a_render_count_is_not_reported_outside_the_render() -> None:
    """Between two renders the column holds the last one's tally; it is not this step's."""
    stored = episode(EpisodeState.SCRIPTING, segments=9, rendered=9)
    shown = progress(stored, job("script"))
    assert shown is not None and shown.segments_rendered == 0


def test_a_ready_episode_has_no_step_and_stops_its_clock_at_publication() -> None:
    stored = episode(
        EpisodeState.READY, segments=9, created=timedelta(minutes=5), published=timedelta(minutes=3)
    )
    shown = progress(stored, None)
    assert shown is not None
    assert (shown.step, shown.stage, shown.steps_done) == (None, "ready", 3)
    assert shown.elapsed_ms == 120_000
    assert (shown.estimate_ms, shown.estimate_samples) == (None, 0)


def test_an_episode_ready_for_longer_than_the_window_reports_nothing() -> None:
    stored = episode(
        EpisodeState.READY,
        created=SETTLED_VISIBLE + timedelta(minutes=10),
        published=SETTLED_VISIBLE + timedelta(seconds=1),
        updated=SETTLED_VISIBLE + timedelta(seconds=1),
    )
    assert reports_progress(stored, now=NOW) is False
    assert progress(stored, None) is None


def test_a_build_that_exhausted_its_retries_still_reports_the_failure() -> None:
    """The window runs from when it gave up, not from when it was asked for.

    An exhausted ladder is ~755s of backoff plus five attempts' runtime plus five Cloud Run
    starts, so it always gives up past minute thirteen. Measured from ``created_at`` the
    ten-minute window had already closed, and the failure panel — the whole reason the
    failed branch exists — never rendered for the commonest way a build fails.
    """
    stored = episode(
        EpisodeState.FAILED,
        segments=4,
        created=timedelta(minutes=16),
        updated=timedelta(minutes=1),
        last_error="Cartesia refused the text",
    )
    assert reports_progress(stored, now=NOW) is True
    shown = progress(stored, job("tts", "failed", attempts=5, error="Cartesia refused the text"))
    assert shown is not None
    assert (shown.step, shown.stage, shown.error) == ("tts", "failed", "Cartesia refused the text")
    # And the clock stops when it gave up rather than running on.
    assert shown.elapsed_ms == 15 * 60 * 1000


def test_a_failure_older_than_the_window_reports_nothing() -> None:
    stored = episode(
        EpisodeState.FAILED,
        created=timedelta(hours=2),
        updated=SETTLED_VISIBLE + timedelta(seconds=1),
        last_error="gave up",
    )
    assert reports_progress(stored, now=NOW) is False
    assert progress(stored, None) is None


# --- going wrong ------------------------------------------------------------------------


def test_a_step_backing_off_after_a_failure_is_retrying_and_says_why_and_when() -> None:
    shown = progress(
        episode(EpisodeState.SCRIPTING, segments=4),
        job("script", attempts=2, error="OpenRouter 429"),
    )
    assert shown is not None
    assert (shown.step, shown.stage) == ("script", "retrying")
    assert (shown.attempt, shown.max_attempts) == (2, DEFAULT_MAX_ATTEMPTS)
    assert shown.error == "OpenRouter 429"
    assert shown.next_attempt_at == NOW + timedelta(seconds=30)


def test_a_first_attempt_waiting_its_turn_is_queued_rather_than_retrying() -> None:
    """A job claimed once and deferred for a busy key carries attempts but no error."""
    shown = progress(episode(EpisodeState.SCRIPTING, segments=4), job("script", attempts=1))
    assert shown is not None and shown.stage == "queued" and shown.error is None


def test_a_failed_episode_names_the_step_that_gave_up_and_why() -> None:
    stored = episode(EpisodeState.FAILED, segments=4, last_error="Cartesia refused the text")
    shown = progress(stored, job("tts", "failed", attempts=5, error="Cartesia refused the text"))
    assert shown is not None
    assert (shown.step, shown.stage) == ("tts", "failed")
    # Two steps behind it, not three: a render that produced nothing is not a finished
    # build, and reporting three would draw a full bar over an episode with no audio.
    assert shown.steps_done == 2
    assert shown.error == "Cartesia refused the text"
    assert shown.waiting_on_worker is False
    # Nothing is outstanding, so nothing is promised about attempts or a next try.
    assert (shown.attempt, shown.next_attempt_at, shown.estimate_ms) == (0, None, None)


def test_a_failed_episode_whose_job_row_was_pruned_still_reports_its_error() -> None:
    stored = episode(EpisodeState.FAILED, last_error="no news items match this rule")
    shown = progress(stored, None)
    assert shown is not None
    assert shown.step is None
    assert (shown.stage, shown.error) == ("failed", "no news items match this rule")
    # **None of three, not three of three.** The step is unknowable — `episodes` records
    # that a stage gave up and never which, and the job row that would have said is gone
    # — and reading "no step" as "every step" drew a *full* bar under "This episode could
    # not be made", which is the one thing the determinate bar exists to avoid saying.
    assert shown.steps_done == 0


def test_a_build_that_stopped_with_no_step_to_name_draws_an_empty_bar_not_a_full_one() -> None:
    """The same rule from the client's side, since the bar is what a person actually sees."""
    stored = episode(EpisodeState.SCRIPTING, segments=4)
    shown = progress(stored, None)
    assert shown is not None
    assert (shown.stage, shown.step, shown.steps_done) == ("failed", "script", 1)


def test_an_unfinished_episode_with_no_job_anywhere_is_reported_stopped_not_queued() -> None:
    """Every stage enqueues the next in the transaction that finishes its own, so this is
    a lost row rather than a gap — and "queued" over an empty queue is motet#38's lie."""
    shown = progress(episode(EpisodeState.SCRIPTING, segments=4), None)
    assert shown is not None
    assert shown.stage == "failed"
    assert shown.error is not None and "Make it again" in shown.error


def test_a_stale_running_row_on_a_finished_stage_does_not_report_a_step_backwards() -> None:
    """A worker that died between committing its work and completing its job leaves one.

    The step comes off the episode, which moved on, not off the job, which did not — and
    the *query* prefers a job on the step's own queue, so in practice the stale row loses
    to the real one. Where it is genuinely all there is, the reading must not claim a
    worker is on the step: `episode_builds` orders the step's queue first precisely so
    that "running" describes the job the step names.
    """
    stored = episode(EpisodeState.RENDERING, segments=6, rendered=2)
    # What `episode_builds` hands back once its ORDER BY has chosen — the tts row, not the
    # stale script one. That the *query* chooses it is a claim about SQL and is pinned
    # against a real Postgres in `test_the_query_prefers_the_job_on_the_steps_own_queue`;
    # this is the reading on top of that answer.
    shown = progress(stored, job("tts"))
    assert shown is not None
    assert (shown.step, shown.stage) == ("tts", "queued")
    # And if the stale row were somehow all there was, the step *still* comes off the
    # episode, which has moved on — so the reading never reports a build going backwards
    # to `script`. The stage would read `running` off that row, which is why choosing
    # between the two rows is the query's job rather than this function's: both rows do
    # exist in the case that produces a stale one, because the work the dead worker
    # committed is what enqueued the tts job.
    stale_only = progress(stored, job("script", "running"))
    assert stale_only is not None
    assert stale_only.step == "tts"


def test_a_long_render_is_never_accused_of_having_no_worker() -> None:
    """A render outlives the heartbeat's freshness window; the worker is paying Cartesia."""
    stored = episode(EpisodeState.RENDERING, segments=20, rendered=11)
    shown = progress(stored, job("tts", "running"), heartbeats=EVERY_QUEUE_GONE)
    assert shown is not None and shown.waiting_on_worker is False


# --- the estimate -------------------------------------------------------------------------


def test_too_few_finished_episodes_is_no_estimate_rather_than_a_made_up_one() -> None:
    assert estimate_ms([]) is None
    assert estimate_ms([90_000] * (ESTIMATE_MIN_SAMPLES - 1)) is None


def test_the_estimate_is_the_median_so_one_slow_build_does_not_move_it() -> None:
    assert estimate_ms([80_000, 90_000, 100_000]) == 90_000
    assert estimate_ms([80_000, 90_000, 100_000, 2_400_000]) == 95_000


def test_a_building_episode_carries_the_estimate_and_what_it_is_made_of() -> None:
    shown = progress(
        episode(EpisodeState.PENDING), job("assemble"), samples=(80_000, 90_000, 100_000)
    )
    assert shown is not None
    assert (shown.estimate_ms, shown.estimate_samples) == (90_000, 3)


# --- the route, over real job rows ---------------------------------------------------------

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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


def shown_for(api: TestClient, episode_id: str) -> dict[str, Any] | None:
    listed = api.get("/v1/episodes", headers=AUTH).json()
    found = next(entry for entry in listed if entry["id"] == episode_id)
    out: dict[str, Any] | None = found["build_progress"]
    return out


def test_an_episode_reports_each_step_as_its_jobs_run(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str
) -> None:
    """queued/assemble → script → tts → ready, off real job rows and a real worker."""
    api.post(
        "/v1/sources/paste",
        json={"title": "A thing happened", "text": "A story about a thing that happened."},
        headers=AUTH,
    )
    # One pass over every queue, which is what `runner all` — the shape both deployed
    # environments run — does: the paste integrates, and every other queue heartbeats on
    # the empty pass it makes. That is what makes the step's own queue answer below.
    for queue in PIPELINE:
        drain(queue, _migrated)

    created = api.post(
        "/v1/episodes", json={"title": "Episode", "max_duration_ms": 1_200_000}, headers=AUTH
    ).json()
    episode_id = created["id"]
    # The create response carries it too, so the screen that asked has it at once.
    assert created["build_progress"]["step"] == "assemble"
    assert created["build_progress"]["stage"] == "queued"
    # A worker swept every queue a moment ago, the assemble queue included, so one is
    # coming for this job — which is the question `waiting_on_worker` answers, per queue.
    assert created["build_progress"]["waiting_on_worker"] is False

    drain(Queue.ASSEMBLE, _migrated)
    shown = shown_for(api, episode_id)
    assert shown is not None
    assert (shown["step"], shown["stage"], shown["steps_done"]) == ("script", "queued", 1)
    assert shown["news_items"] == 1

    drain(Queue.SCRIPT, _migrated)
    shown = shown_for(api, episode_id)
    assert shown is not None
    assert (shown["step"], shown["stage"], shown["steps_done"]) == ("tts", "queued", 2)
    assert shown["claims"] > 0
    assert shown["waiting_on_worker"] is False

    drain(Queue.TTS, _migrated)
    shown = shown_for(api, episode_id)
    assert shown is not None
    assert (shown["step"], shown["stage"], shown["steps_done"]) == (None, "ready", 3)
    assert shown["elapsed_ms"] >= 0
    assert shown["segments_rendered"] == 0  # not the render's step any more
    assert api.get(f"/v1/episodes/{episode_id}", headers=AUTH).json()["state"] == "ready"
    # Every segment was recorded on the way through, on the side connection.
    row = db.execute("SELECT rendered_segments FROM episodes").fetchone()
    assert row is not None and row["rendered_segments"] == shown["news_items"]


def test_a_worker_reports_its_render_segment_by_segment(
    api: TestClient,
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle_tts`` writes the tally on a side connection, so it is visible **mid-render**.

    Observed from *another* connection while the render's own transaction is still open,
    which is the one property the side connection exists for: asserted after the drain, the
    same assertion would pass with the write on ``context.conn``.
    """
    seen: list[int] = []

    def watch(_text: str) -> Any:
        with psycopg.connect(_migrated, row_factory=dict_row) as side:
            row = side.execute("SELECT rendered_segments FROM episodes").fetchone()
        assert row is not None
        seen.append(row["rendered_segments"])
        return real_synthesize(_text)

    from motet_inference import registry as inference_registry

    real_synthesize = inference_registry.get_stages().speech_synthesizer.synthesize
    for index, text in enumerate(
        ("First story, about one thing.", "Second story, about a different thing.")
    ):
        api.post("/v1/sources/paste", json={"title": f"Story {index}", "text": text}, headers=AUTH)
    drain(Queue.INTEGRATE, _migrated)
    api.post("/v1/episodes", json={"title": "Episode", "max_duration_ms": 1_200_000}, headers=AUTH)
    drain(Queue.ASSEMBLE, _migrated)
    drain(Queue.SCRIPT, _migrated)
    monkeypatch.setattr(
        type(inference_registry.get_stages().speech_synthesizer),
        "synthesize",
        lambda _self, text: watch(text),
    )
    drain(Queue.TTS, _migrated)

    # One reading per segment, taken before that segment was synthesized and from a
    # connection the render's transaction cannot have committed to yet.
    assert seen == list(range(len(seen)))
    assert len(seen) >= 2, seen
    rendered = db.execute("SELECT rendered_segments FROM episodes").fetchone()
    assert rendered is not None and rendered["rendered_segments"] == len(seen)


def test_a_reporter_that_cannot_write_never_fails_the_render_and_stops_trying(
    api: TestClient,
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side connection's two promises, and neither was pinned by anything.

    A render is minutes of billed Cartesia calls and the count is a number on a screen, so
    the order of those two is not a judgement call: every failure of the reporter is
    swallowed and the render finishes. And a *first* failure turns the reporter off rather
    than being retried once per segment, because whatever refused the first write — a role
    without ``UPDATE``, a column an older database has not got, a lock timeout — is not
    going to start working inside the same render.

    Deleting either the ``except`` or the ``live = False`` in ``_render_reporter`` left
    every other test green, which is why this one is here: the behaviour it protects is
    only visible when something is already broken.
    """
    calls: list[int] = []

    def refuse(_conn: Any, _episode_id: str, count: int) -> None:
        calls.append(count)
        raise psycopg.OperationalError("the worker role may not update episodes")

    monkeypatch.setattr(handlers.repo, "record_rendered_segments", refuse)

    for index, text in enumerate(
        ("First story, about one thing.", "Second story, about a different thing.")
    ):
        api.post("/v1/sources/paste", json={"title": f"Story {index}", "text": text}, headers=AUTH)
    drain(Queue.INTEGRATE, _migrated)
    created = api.post(
        "/v1/episodes", json={"title": "Episode", "max_duration_ms": 1_200_000}, headers=AUTH
    ).json()
    drain(Queue.ASSEMBLE, _migrated)
    drain(Queue.SCRIPT, _migrated)
    drain(Queue.TTS, _migrated)

    # The audio was made and published, which is the promise that matters.
    assert api.get(f"/v1/episodes/{created['id']}", headers=AUTH).json()["state"] == "ready"
    # And the reporter asked exactly once — the reset to zero — rather than once per
    # segment against something that was never going to answer.
    assert calls == [0]
    row = db.execute("SELECT rendered_segments FROM episodes").fetchone()
    assert row is not None and row["rendered_segments"] == 0


def test_a_queued_episode_says_so_when_no_worker_has_run_in_five_minutes(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str
) -> None:
    """Production's state until the drain trigger was turned on: a queue nobody drains.

    The episode is queued and looks exactly like one a worker is about to pick up, which is
    the never-infer-"no errors"-from-"no data" trap wearing a progress bar (motet#38).
    """
    api.post(
        "/v1/sources/paste", json={"title": "A story", "text": "Something happened."}, headers=AUTH
    )
    drain(Queue.INTEGRATE, _migrated)
    created = api.post(
        "/v1/episodes", json={"title": "Episode", "max_duration_ms": 1_200_000}, headers=AUTH
    ).json()
    # Every worker stopped right after integrating: the heartbeats go stale where they are.
    db.execute("UPDATE worker_heartbeats SET last_seen_at = now() - interval '10 minutes'")
    db.commit()

    shown = shown_for(api, created["id"])
    assert shown is not None
    assert (shown["step"], shown["stage"]) == ("assemble", "queued")
    assert shown["waiting_on_worker"] is True


def test_the_three_spellings_of_the_pipeline_queues_agree() -> None:
    """`motet-db` cannot import `motet-workers`, so it spells the queue names itself.

    Three copies exist by necessity — the repository's ``EPISODE_QUEUES``, migration
    0025's index predicate, and the step mapping here — and a queue added to the pipeline
    and forgotten in one of them is a stage a screen calls "queued" forever. This is the
    one place that can see all three, because tests may import both packages.
    """
    from motet_db.repo import EPISODE_QUEUES

    assert EPISODE_QUEUES == (Queue.ASSEMBLE.value, Queue.SCRIPT.value, Queue.TTS.value)
    assert set(STEP_FOR_QUEUE) == set(EPISODE_QUEUES)
    assert set(STEP_FOR_STATE.values()) == set(STEP_ORDER) == set(STEP_FOR_QUEUE.values())
    migration = (
        Path(__file__).resolve().parents[2] / "db/migrations/0025_episode_build_progress.sql"
    ).read_text()
    assert f"queue IN ({', '.join(repr(q) for q in EPISODE_QUEUES)})" in migration


def test_the_query_prefers_the_job_on_the_steps_own_queue(
    db: psycopg.Connection[Any],
) -> None:
    """The LATERAL's ``ORDER BY``, against a real Postgres and two real rows.

    This is the ordering the whole query is shaped around, and it is not a claim a pure
    function can make: a worker that died between committing its work and completing its
    job leaves a stale ``running`` row on the stage it *finished*, beside the ``ready``
    row of the stage its own commit enqueued. Both rows exist at once. Taking the stale
    one's stage would announce "a worker is running this" over a job nothing has claimed
    — motet#38's lie, in exactly the case ``waiting_on_worker`` exists to catch.
    """
    from motet_db import repo
    from motet_workers import jobs

    episode_id = repo.create_episode(
        db, user_id="motet-owner", title="two rows", max_duration_ms=600_000
    )
    repo.set_episode_state(db, episode_id, EpisodeState.RENDERING)
    # The stale one first, and claimed, so it is both older *and* `running` — every key
    # ahead of `id DESC` would pick it if the queue key were not there.
    jobs.enqueue(db, Queue.SCRIPT, {"episode_id": episode_id})
    assert jobs.claim(db, Queue.SCRIPT) is not None
    jobs.enqueue(db, Queue.TTS, {"episode_id": episode_id})
    db.commit()

    build = repo.episode_builds(db, [episode_id])[episode_id]
    assert build.job is not None
    assert (build.job.queue, build.job.state) == ("tts", "ready")

    # And a `failed` row loses to a live one on the same queue, so a step that is being
    # retried reads as retrying rather than as the last attempt that gave up.
    db.execute("UPDATE jobs SET state = 'failed' WHERE queue = 'tts'")
    jobs.enqueue(db, Queue.TTS, {"episode_id": episode_id})
    db.commit()
    again = repo.episode_builds(db, [episode_id])[episode_id]
    assert again.job is not None
    assert (again.job.queue, again.job.state) == ("tts", "ready")


def test_the_episode_job_read_is_answered_by_the_partial_index(
    db: psycopg.Connection[Any],
) -> None:
    """Polled every three seconds while anything is being made; a scan here is motet#49.

    **Three things about how this is written are the test**, and the first version of it
    had none of them — it EXPLAINed a hand-written ``SELECT 1 FROM jobs WHERE …`` over an
    empty table, which is a query that tests itself and a table on which a sequential
    scan is the *right* plan.

    *The real statement*, LATERAL and all, because the claim is about that one.

    *A seeded table*, so that taking the index is a choice the planner made rather than a
    tie between two cheap plans.

    *A forced generic plan*, which is the mode in which the two spellings of the queue
    filter could differ at all: a generic plan holds no parameter values, so a bound
    ``queue = ANY($n)`` gives the planner nothing to prove the partial index's predicate
    from, where literals give it everything. psycopg prepares after five executions, so
    generic is what a polled route settles into.

    **What this does not claim, having been measured:** that the bound spelling would
    scan *here*. On a bare `SELECT … WHERE queue = ANY($1) AND …` it does — forced
    generic, 20,000 rows, cost 1,214 on ``Seq Scan`` against 271 on the index. On this
    statement both spellings take the index, because the ``unnest`` join drives a nested
    loop keyed on ``payload ->> 'episode_id' = e.id`` and the index wins on that alone.
    The literals are the spelling that cannot go wrong; `_EPISODE_QUEUE_LITERALS` says so
    in those words rather than claiming a regression this query was seen to have.
    """
    from motet_db import repo
    from motet_workers import jobs

    episode_id = repo.create_episode(
        db, user_id="motet-owner", title="a full queue", max_duration_ms=600_000
    )
    db.execute(
        """
        INSERT INTO jobs (queue, payload, run_at)
        SELECT 'tts', jsonb_build_object('episode_id', 'ep_seed_' || g), now()
        FROM generate_series(1, 20000) g
        """
    )
    jobs.enqueue(db, Queue.ASSEMBLE, {"episode_id": episode_id})
    db.execute("ANALYZE jobs")

    db.execute("SET plan_cache_mode = force_generic_plan")
    try:
        plan = "\n".join(
            str(row["QUERY PLAN"])
            for row in db.execute(
                "EXPLAIN " + repo._EPISODE_BUILD_SQL, ([episode_id],), prepare=True
            ).fetchall()
        )
    finally:
        db.execute("SET plan_cache_mode = auto")
    assert "jobs_episode_idx" in plan, plan
    assert "Seq Scan on jobs" not in plan, plan


def test_the_queue_names_reach_the_statement_as_literals() -> None:
    """The structural half of the argument above, and the only deterministic half.

    The EXPLAIN test would stay green if the queues went back to being bound, because on
    this statement both spellings take the index. So what actually pins the spelling is
    reading it: the names are in the SQL text, and the statement takes exactly the one
    parameter — the episode ids — that it is called with.
    """
    from motet_db.repo import _EPISODE_BUILD_SQL, EPISODE_QUEUES

    assert f"queue IN ({', '.join(repr(q) for q in EPISODE_QUEUES)})" in _EPISODE_BUILD_SQL
    assert _EPISODE_BUILD_SQL.count("%s") == 1
