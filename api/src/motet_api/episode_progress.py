"""Where an episode is between "make it" and a file to play, as one reading both clients render.

``POST /v1/episodes`` returns in ``pending`` and the rest happens on three queues —
``assemble``, ``script``, ``tts`` — so an episode moves through four states over a minute or
several. Until this existed the only thing a client could show was ``episodes.state``, which
is a bare word: it cannot say whether a worker has the job or whether it is still waiting for
one, it cannot say that the stage is on its fourth attempt, and it cannot say how far through
the render it is. That is motet#136's Gmail complaint, one surface along.

Two records answer it and neither alone can. The **episode row** says which stage it is at,
and — for a render — how many segments are done (migration 0025). The **job queue** says what
is happening to that stage: queued, running, or backing off after a failure that has not yet
used up its attempts. Postgres being the queue as well as the datastore is what makes that a
join rather than a second system to ask.

**Two fields rather than one, and the split is deliberate.**
:class:`~.schemas.SourceSyncProgress` folds both into a single ``stage``, because a sync has
exactly one kind of work in it. An episode has three, so ``step`` says *which work* and
``stage`` says *what is happening to it* — two small vocabularies rather than one nine-member
enum with half its members verbs and half nouns. A client branches on ``stage`` for its tone
and reads ``step`` for its noun.

Pure, over values, so every state is a unit test with no database. The judgements in it are
:data:`WORKER_FRESH` — the Processing panel's own five minutes, because a queued episode with
no worker alive is an episode nothing will build (motet#38) — and
:data:`ESTIMATE_MIN_SAMPLES`, which is what stops an estimate from being invented.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from motet_db import EpisodeState
from motet_db.models import StoredEpisode
from motet_db.repo import EpisodeBuild, EpisodeJob
from motet_workers import DEFAULT_MAX_ATTEMPTS

from .schemas import EpisodeBuildProgress
from .sync_progress import WORKER_FRESH, WORKER_STARTING

Step = Literal["assemble", "script", "tts"]
Stage = Literal["queued", "running", "retrying", "ready", "failed"]

#: :data:`WORKER_FRESH` and :data:`WORKER_STARTING` are the sync panel's, imported rather
#: than restated: one number per question, on both pipelines. Where a worker is one-shot
#: and started on demand — production — its heartbeat is *always* stale at the moment an
#: episode is created, and the four-minute window is what stops that reading as "nothing
#: will build this" over a container that is booting because of that very request. The
#: sync panel's docstring (motet#137) is the argument in full.

#: How long a finished or failed build keeps reporting how it went.
#:
#: Shorter than a sync's hour, because unlike a sync there is something to *do* with the
#: result the moment it lands: a ready episode is a Play button and a failed one is an
#: error the episode row carries anyway. What this window is for is the person who pressed
#: "Make it" and is still watching — it should say "ready · took 1m 33s" rather than
#: vanishing the instant it succeeds, which would read as the screen losing its place.
SETTLED_VISIBLE = timedelta(minutes=10)

#: How few finished episodes is too few to estimate from.
#:
#: Three is less a sample size than a guard against the degenerate cases: with one, the
#: "estimate" is that episode; with two, a first build that waited out a cold Cloud Run
#: start sets the number for the next one. Below this the clients show elapsed time and the
#: step and no estimate at all, which is the honest answer when there is no basis for one.
ESTIMATE_MIN_SAMPLES = 3

#: How many recent episodes the estimate is the median of. Recent, because the two largest
#: terms in a build — how long Cloud Run takes to start a worker, and how much backlog there
#: is to get through — are facts about the deployment on the day rather than constants.
ESTIMATE_SAMPLE_SIZE = 10

#: The steps in the order they run. ``steps_done`` counts along it.
STEP_ORDER: tuple[Step, ...] = ("assemble", "script", "tts")

#: Which step an episode in each state is waiting on.
#:
#: **The episode row is the primary truth for this, not the job row**, and that is the one
#: thing to get right here. A worker that dies between committing its work and completing
#: its job leaves a stale ``running`` row on the stage it *finished*, beside the ``ready``
#: row of the stage it handed on to (motet#50, motet#53). Naming the step off the job would
#: report such an episode as having gone backwards; naming it off the state cannot, because
#: the state and the next stage's job row were written in one transaction.
STEP_FOR_STATE: dict[EpisodeState, Step] = {
    EpisodeState.PENDING: "assemble",
    EpisodeState.SCRIPTING: "script",
    EpisodeState.RENDERING: "tts",
}

#: The states an episode is still being built in, as the wire strings
#: :class:`~motet_db.repo.EpisodeBuild` carries. The keys of :data:`STEP_FOR_STATE` —
#: named so a caller can ask the question without depending on it being a mapping.
BUILDING_STATE_NAMES: frozenset[str] = frozenset(state.value for state in STEP_FOR_STATE)

#: Which step a queue name is, for the one case the state cannot answer: a ``failed``
#: episode, whose row records *that* a stage gave up and never *which*. Keyed on
#: :data:`motet_db.repo.EPISODE_QUEUES`' own names.
STEP_FOR_QUEUE: dict[str, Step] = {"assemble": "assemble", "script": "script", "tts": "tts"}

#: The queue each step's work sits on — :data:`STEP_FOR_QUEUE` read the other way.
#:
#: Used for the heartbeat, and that is a real distinction rather than tidiness. A worker
#: is started per queue in the ``runner <queue> --poll-seconds N`` shape, which AGENTS.md
#: keeps as a supported deployment, so "some worker somewhere ran" does not answer "will
#: anything pick *this* job up". Asking the step's own queue does. In the ``runner all``
#: shape every pass heartbeats every queue, so the two readings agree there.
QUEUE_FOR_STEP: dict[Step, str] = {step: queue for queue, step in STEP_FOR_QUEUE.items()}

#: What a client is told when an episode is in no terminal state and has no job anywhere.
NO_JOB_ERROR = "This episode has no job on any queue, so nothing will move it. Make it again."


def estimate_ms(samples: Sequence[int]) -> int | None:
    """A rough total build time from recent ones, or ``None`` when there is no basis.

    The **median** rather than the mean, because the distribution has a long right tail and
    no left one: the floor is the work itself, and everything that goes wrong — a cold worker
    start, a retry up the backoff ladder, an unusually full backlog — only ever adds. One
    forty-minute outlier would drag a mean of five past every build it is meant to predict.
    """
    if len(samples) < ESTIMATE_MIN_SAMPLES:
        return None
    return int(statistics.median(samples))


def settled_at(
    state: EpisodeState, *, published_at: datetime | None, updated_at: datetime
) -> datetime | None:
    """When a finished build stopped, or ``None`` for one still going.

    A ready episode settled when it published. **A failed one settled when it last
    changed**, and getting that from ``created_at`` instead is the bug this function
    exists to have one answer to: `jobs.BACKOFF_SECONDS` plus
    `jobs.DEFAULT_MAX_ATTEMPTS` means an exhausted retry ladder spends about 755 seconds
    in backoff alone, before each attempt's own runtime and before the Cloud Run
    scheduling latency this file's own header measures at 72 seconds a go. So a build
    that fails the ordinary way always gives up past minute thirteen — comfortably outside
    a ten-minute window counted from creation, which would have hidden the failure panel
    in exactly the case it was written for.
    """
    if state is EpisodeState.READY:
        return published_at or updated_at
    if state is EpisodeState.FAILED:
        return updated_at
    return None


def reports_progress(episode: StoredEpisode, *, now: datetime) -> bool:
    """Whether this episode has a build worth describing.

    Asked *before* the job queue is read, which is what keeps a shelf of fifty ready
    episodes from asking about fifty of them. An episode still building always does; one
    that settled is described by its own state and duration past :data:`SETTLED_VISIBLE`.

    It reads the *list route's* copy of the episode, which may be a snapshot older than
    the one :func:`build_progress` then works from — so this is deliberately the more
    permissive of the two: an episode that settled since is still admitted here and
    correctly dropped there, and never the other way round.
    """
    stopped = settled_at(
        episode.state,
        published_at=episode.published_at,
        updated_at=episode.updated_at or episode.created_at,
    )
    return stopped is None or now - stopped <= SETTLED_VISIBLE


def build_progress(
    episode: StoredEpisode,
    build: EpisodeBuild | None,
    *,
    now: datetime,
    heartbeats: Mapping[str, datetime],
    samples: Sequence[int] = (),
    drain_trigger_enabled: bool = False,
) -> EpisodeBuildProgress | None:
    """The step, the stage, the counts and the clock — or ``None`` when there is nothing to say.

    ``build`` is the state and the job read in **one** statement
    (:func:`motet_db.repo.episode_builds`), and the state it carries wins over the
    ``episode`` row's, which a list route read in an earlier snapshot. ``episode`` is still
    what supplies the counts and the creation time: those are stale by at most one poll and
    never produce a wrong *verdict*, where a stale state does.

    ``drain_trigger_enabled`` is whether this deployment starts a worker when it enqueues
    (``MOTET_DRAIN_TRIGGER``), which is what lets a queued assemble job younger than
    :data:`WORKER_STARTING` read as a worker *starting* rather than a worker missing. It is
    a claim that this API asked for one, never that Cloud Run obliged: the nudge is
    fire-and-forget and leaves no record, so the window bounds it in both directions.

    ``None`` for an episode that settled longer ago than :data:`SETTLED_VISIBLE`, and for
    one that no longer exists — ``build`` is ``None`` when the row was deleted between the
    two statements.
    """
    if build is None:
        return None
    state = EpisodeState(build.state)
    stopped = settled_at(state, published_at=build.published_at, updated_at=build.updated_at)
    if stopped is not None and now - stopped > SETTLED_VISIBLE:
        return None
    job = build.job

    stage: Stage
    if state is EpisodeState.READY:
        stage = "ready"
    elif state is EpisodeState.FAILED:
        stage = "failed"
    elif job is None:
        # Not finished, and no job on any of the three queues — read in the same statement
        # as the state, so this is not the render that finished a millisecond ago. Every
        # stage enqueues the next in the transaction that completes its own, so it is a row
        # that was lost, and nothing will ever move this episode again. Reported as failed
        # with a sentence saying so, because "queued" over a queue with nothing in it is
        # exactly the lie motet#38 is about.
        stage = "failed"
    elif job.state == "running":
        stage = "running"
    elif job.attempts > 0 and job.last_error:
        stage = "retrying"
    else:
        stage = "queued"

    # The step is the episode's own, never the job's: a stale `running` row sits on the
    # stage a dead worker *finished*, and naming the step off it would report the episode
    # as having gone backwards. `ready` has no outstanding step at all — not even when a
    # stale tts row is still lying around — and `failed`'s comes off the job, because the
    # episode row records *that* a stage gave up and never *which*.
    step: Step | None = None
    if stage != "ready":
        step = STEP_FOR_STATE.get(state) or (STEP_FOR_QUEUE.get(job.queue) if job else None)

    in_flight = stage in ("queued", "running", "retrying")
    # How many steps are *behind* it. For a failed episode that is the index of the step
    # that stopped it — a render that gave up got two steps in, and reporting three would
    # draw a full bar over an episode that produced nothing. Only `ready` is three of three.
    steps_done = len(STEP_ORDER) if step is None else STEP_ORDER.index(step)

    # What each stage has to show for itself. Assembly writes one segment per chosen story
    # and scripting rewrites them with text and claims, so `news_items` is real from
    # `scripting` onward and `claims` from `rendering` onward. Neither is invented earlier:
    # before assembly there are no segments, and both are zero.
    news_items = len(episode.segments)
    claims = sum(len(segment.claims) for segment in episode.segments)

    error: str | None = None
    if stage == "failed":
        error = episode.last_error or (job.last_error if job else None) or NO_JOB_ERROR
    elif stage == "retrying" and job is not None:
        error = job.last_error

    elapsed = max(0, round(((stopped or now) - episode.created_at).total_seconds() * 1000))

    unattended = stage in ("queued", "retrying")
    alive = _worker_alive(heartbeats.get(QUEUE_FOR_STEP[step]) if step else None, now)
    starting = (
        unattended
        and not alive
        and _worker_starting(step, job, now=now, enabled=drain_trigger_enabled)
    )

    return EpisodeBuildProgress(
        step=step,
        stage=stage,
        steps_done=steps_done,
        steps_total=len(STEP_ORDER),
        news_items=news_items,
        claims=claims,
        # Only while the render is the step: before it the column holds the previous
        # attempt's tally and after it the number is the segment count, and either read as
        # "recorded so far" would be a count of work that is not happening.
        segments_rendered=min(episode.rendered_segments, news_items) if step == "tts" else 0,
        elapsed_ms=elapsed,
        estimate_ms=estimate_ms(samples) if in_flight else None,
        estimate_samples=len(samples) if in_flight else 0,
        attempt=job.attempts if job is not None and in_flight else 0,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        next_attempt_at=job.run_at if stage == "retrying" and job is not None else None,
        error=error,
        # A running job is being worked on by definition, whatever the heartbeat says: a
        # long render is exactly the job that outlives the freshness window, and accusing
        # the worker that is at that moment paying Cartesia is the mistake the episode
        # screen's banner already avoids by hand. And a worker that is *starting* is not
        # one that is missing — the two are mutually exclusive, as on the sync panel.
        waiting_on_worker=unattended and not alive and not starting,
        worker_starting=starting,
    )


def _worker_alive(seen: datetime | None, now: datetime) -> bool:
    return seen is not None and now - seen <= WORKER_FRESH


def _worker_starting(
    step: Step | None, job: EpisodeJob | None, *, now: datetime, enabled: bool
) -> bool:
    """Whether a worker was asked for, recently enough that it may still be booting.

    Read off the **assemble** job alone, because that is the one this API enqueues and
    nudges for (``create_episode`` writes it and arms ``DrainReason.EPISODE``). The script
    and tts jobs are written by the worker itself, which fires no trigger — and a worker
    that just wrote one is alive, so every caller gates on the heartbeat being stale
    first. That is :func:`motet_api.sync_progress._worker_starting`'s argument with the
    poll job swapped for the assemble job, and the same two facts carry it: ``drain``
    heartbeats before each claim, and :data:`WORKER_STARTING` sits a minute inside
    :data:`WORKER_FRESH`.
    """
    if not enabled or step != "assemble" or job is None or job.queue != "assemble":
        return False
    return timedelta() <= now - job.created_at <= WORKER_STARTING
