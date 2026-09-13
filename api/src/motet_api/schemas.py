"""Request and response models — the shapes that become the OpenAPI contract.

This is the seam between the API and every client. Changing a model here changes
``openapi.yaml`` and the generated TypeScript client, and CI fails if either is stale.

**Invariant 1 is why this file matters more than it looks like it should.** No client ever
speaks a vendor protocol; it speaks this. Which means a provider swap is a change to the
adapters and to nothing a client can see — and that only stays true if the vendor-shaped
details never leak into these models.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness plus enough wiring detail to tell 'quiet' from 'unmonitored'."""

    status: str = Field(description="'ok' when the process is serving")
    service: str = Field(description="OTel service name this process reports as")
    revision: str | None = Field(
        description=(
            "The tadasant/motet commit SHA this build was made from — the deploy's "
            "image tag, read back out of the OTel 'service.version' resource attribute. "
            "Not a Cloud Run revision name. null means nothing set it — a laptop or a "
            "bare 'docker run' rather than a deployment — or that what was set is not a "
            "shape this public route will repeat, which the process says at ERROR on "
            "startup. Reported because 'is the pin bump actually live' is the first "
            "question asked after a deploy, and until now the only answers were diffing "
            "the served OpenAPI document — which cannot see a commit that changed only "
            "behaviour — or querying the obs stack for a log line this service does not "
            "emit per request."
        ),
    )
    telemetry_configured: bool = Field(
        description="Whether OTLP export is configured. False means telemetry is a no-op."
    )
    telemetry_exporting: bool = Field(
        description=(
            "Whether this process actually installed an exporter, which is a different "
            "question from whether the variables were set. False with "
            "telemetry_configured true means the wiring is right and the SDK did not "
            "start — check the startup log."
        )
    )
    errors_configured: bool = Field(
        description="Whether error reporting is configured. False means errors go nowhere."
    )
    authenticated: bool = Field(
        description=(
            "Whether /v1 requires a bearer token. False means this deployment is open to "
            "anyone who can reach it — legitimate on a laptop, a mistake anywhere else."
        )
    )
    login_configured: bool = Field(
        description=(
            "Whether signing in with Google can succeed for anyone. False means either no "
            "allowlist is set — which denies everybody, deliberately — or, in real mode, "
            "no Google OAuth client is configured. Reported for the same reason as "
            "'authenticated': a login that denies silently looks exactly like one nobody "
            "has tried."
        )
    )
    vault_backend: str = Field(
        description=(
            "Which credential vault this process resolved: 'kms' or 'local'. 'local' in a "
            "deployed environment is a misconfiguration the process refuses to serve "
            "under — see vault_ready."
        )
    )
    vault_ready: bool = Field(
        description=(
            "Whether a Gmail refresh token could be sealed if one arrived. False means "
            "connecting a mailbox will fail at the last step of the consent flow, after "
            "the provider has already issued a token. Reported for the same reason as "
            "'login_configured': the vault is only ever exercised by a human finishing a "
            "consent flow, so a broken one and an untried one look identical from "
            "outside. It does not call Cloud KMS — this route is unauthenticated, and a "
            "billed vendor call per request would be a free way to spend money."
        )
    )
    drain_trigger: bool = Field(
        description=(
            "Whether this process is configured to ask Cloud Run to start a worker "
            "execution when a request enqueues work, rather than leaving it for the next "
            "worker run. Configured, not proven: where the environment has no run.invoker "
            "grant every ask is refused, and motet.api.drain_triggers{outcome} on the obs "
            "stack is what says whether asks succeed. False means MOTET_DRAIN_TRIGGER is "
            "off — the default — or that it is on and the job could not be resolved or "
            "google-auth is missing from the image, both of which the process says at "
            "ERROR on startup. Reported for the same reason as 'vault_ready': an inert "
            "trigger and a working one look identical from outside. Where the job lives is "
            "deliberately not reported; it is topology, and this route is public."
        )
    )
    inference_mode: str = Field(
        description="'fake' or 'real'. 'fake' means no vendor is ever called."
    )


class PasteRequest(BaseModel):
    """A blob of text pasted in by hand — Phase 1's only ingestion route."""

    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1)


class SourceItemResponse(BaseModel):
    id: str
    title: str
    state: str = Field(
        description="'pending' until a worker integrates it, then 'integrated' or 'failed'."
    )


class HeldSourceItemResponse(BaseModel):
    """A source item that is extracted and waiting for the owner to say "ingest now".

    A connected source polls, fetches and extracts on its own — the deterministic, free
    half — and stops before ``integrate``, the first stage that spends inference. Held is
    ``state = 'pending'`` with no integrate job, and this is the list of those. A paste
    never lingers here: it queues its job on arrival.
    """

    id: str
    title: str
    source_id: str
    source_kind: str = Field(description="'gmail' or 'paste'.")
    source_name: str
    received_at: datetime = Field(
        description=(
            "When the message says it was sent (its Date header, clamped to now); when it "
            "was stored if it had no usable one. For a paste, when it was pasted."
        )
    )
    chars: int = Field(description="Length of the extracted text.")
    preview: str = Field(description="The first ~200 characters, whitespace collapsed.")


class SourceItemIdsRequest(BaseModel):
    """Which held source items to act on. At most as many as the held list returns."""

    ids: list[str] = Field(min_length=1, max_length=500)


class IntegrateResponse(BaseModel):
    queued: int = Field(description="Ids that were held and now have an integrate job.")
    skipped: int = Field(
        description=(
            "Ids that did not qualify: unknown, another user's, already queued, "
            "already integrated, or dismissed. Never an error."
        )
    )


class DismissResponse(BaseModel):
    dismissed: int = Field(description="Ids that were held and are now dismissed.")
    skipped: int = Field(
        description=(
            "Ids that did not qualify: unknown, another user's, already queued or "
            "integrated, or already dismissed. Never an error."
        )
    )


class SourceItemPulledStage(BaseModel):
    """Stage 1 of a source item's life: what the deterministic scrape pulled in.

    Everything here was produced without a model — a poll, a fetch, and
    ``motet_sources.extract``. ``text`` is the extracted text, not the raw message: the
    RFC 822 bytes are never stored, which ``raw_stored`` says out loud so that the UI does
    not call the extracted text "the email".
    """

    source_id: str
    source_kind: str = Field(description="'gmail' or 'paste'.")
    source_name: str
    external_id: str | None = Field(
        description="The provider's own id for the message; null for a paste."
    )
    received_at: datetime = Field(
        description="When the message says it was sent; see HeldSourceItemResponse."
    )
    stored_at: datetime = Field(description="When the source item row was written.")
    chars: int = Field(description="Length of the extracted text.")
    text: str = Field(description="The extracted text, in full.")
    raw_stored: bool = Field(
        description="Whether the raw bytes the text was extracted from are kept. Always false."
    )


class SourceItemJobResponse(BaseModel):
    """The newest ``integrate`` job for a source item, as the queue holds it."""

    id: int
    state: str = Field(description="'ready', 'running', 'done' or 'failed'.")
    attempts: int
    max_attempts: int = Field(description="The ceiling the queue counts to.")
    run_at: datetime = Field(description="When the job is (or was) due.")
    locked_at: datetime | None = Field(description="When a worker last touched its lease.")
    created_at: datetime
    updated_at: datetime
    last_error: str | None
    work_committed: bool = Field(
        description="Whether the handler's work landed durably (the work fence), even if "
        "the job row has not been completed yet."
    )


class DedupDecisionResponse(BaseModel):
    """Why dedup put this source item where it did, as recorded at the time.

    ``relation``, ``reason``, ``candidate_id`` and ``model`` are the first pass's answer and
    are null when the integrator reported none. ``basis`` names the step the outcome rests
    on. ``title`` and ``summary`` are the news item's copy as this decision left it; the
    news item's own are rewritten by every later merge.
    """

    relation: str | None = Field(description="'same_event', 'related' or 'unrelated'.")
    reason: str | None = Field(description="The first pass's one-sentence comparison.")
    candidate_id: str | None = Field(
        description="The news item the first pass judged closest, as it named it."
    )
    candidate_title: str | None = Field(
        description=(
            "That news item's current title; null when there is no candidate or the id "
            "named is not one of the caller's news items."
        )
    )
    model: str | None = Field(description="What answered: an OpenRouter slug, or 'fake'.")
    basis: str = Field(
        description=(
            "'first_pass', 'second_look' (a focused re-ask decided), or 'title_backstop' "
            "(dedup said new, and an unread story already carried the same title)."
        )
    )
    title: str | None = Field(description="The news item's title as this decision wrote it.")
    summary: str | None = Field(description="Likewise the summary.")
    decided_at: datetime


class ProcessingStepResponse(BaseModel):
    """One step of stage 2. Dedup is the only step today; enrichment steps will join it.

    ``status`` follows the step's job: ``queued`` (first attempt due, or a retry backing
    off), ``running``, ``done`` or ``failed``. ``cost_recorded`` is false: the step's spend
    is logged beside the source item id and metered per stage, never stored per item.
    """

    step: str = Field(description="'dedup'.")
    status: str = Field(description="'queued', 'running', 'done' or 'failed'.")
    job: SourceItemJobResponse | None
    finished_at: datetime | None = Field(description="When the step completed, if it has.")
    error: str | None = Field(
        description="The source item's recorded error, or the job's last one while retrying."
    )
    outcome: str | None = Field(
        description=(
            "'new' if this source item created its news item, 'merged' if it was folded "
            "into one that already existed; null until done."
        )
    )
    decision: DedupDecisionResponse | None = Field(
        description="Null until done, and for items integrated before decisions were recorded."
    )
    cost_recorded: bool = Field(description="Whether this step's spend is stored. Always false.")


class SourceItemNewsItemResponse(BaseModel):
    """Stage 3: the deduped news item this source item feeds."""

    id: str
    title: str
    summary: str
    read: bool
    source_count: int = Field(description="How many source items back this story.")
    position: int = Field(description="This source item's position among them; 0 created it.")


class SourceItemDetailResponse(BaseModel):
    """One source item across its three stages: pulled in, processed, news item.

    A read over ``source_items``, the newest ``integrate`` job and ``news_item_sources``.
    ``processed`` is a list so that enrichment steps can join dedup without a new shape;
    it is empty while the item is held or once it is dismissed, and ``news_items`` is
    empty until dedup has run.
    """

    id: str
    title: str
    state: str = Field(description="'pending', 'integrated', 'failed' or 'dismissed'.")
    status: str = Field(
        description=(
            "'held' (pending, nobody has asked for inference), 'queued', 'running', "
            "'done', 'failed' or 'dismissed'."
        )
    )
    pulled: SourceItemPulledStage
    processed: list[ProcessingStepResponse]
    news_items: list[SourceItemNewsItemResponse] = Field(
        description="Zero or one today — a source item belongs to at most one news item."
    )


class IngestionItemResponse(BaseModel):
    """One ingested item that has not settled into the backlog yet — and why not.

    This exists because "pending" used to be a thing the system knew and never said. A
    paste was accepted, queued, retried, and eventually abandoned entirely inside the
    worker, and the only surface that could have shown any of it — the backlog — lists
    news items, which is precisely what a failed item never becomes.

    ``attempts`` and ``next_attempt_at`` are here so that *retrying* and *stuck* are
    distinguishable. They are not the same thing to a person standing there waiting, and
    a spinner that means both is a spinner that means neither.

    **Not always a source item.** A mailbox message whose fetch failed has no
    ``source_items`` row — that is written when extraction succeeds — so it is reported
    from its extract job, under a synthesized ``id`` and a ``title`` naming the provider's
    message id. Every other field means the same thing either way. Ids are
    opaque to clients and nothing addresses this route's rows, so the two shapes are one
    response model rather than two (motet#35).

    **``last_error`` is the exception the stage raised, unedited, and that is the decision
    rather than an oversight.** It is a new egress: an httpx error names the base URL it
    dialled, a psycopg one names the database host. The caller is the deployment's single
    owner behind ``require_caller`` — the same person who reads the obs stack, where the
    identical string already goes — so there is no reader here who could not already see
    it. Mapping unknown exceptions to a generic string would buy nothing from that reader
    and would hand them back the "Failed", with no reason, that this whole surface exists
    to replace. Revisit it when there is more than one account (Phase 3): at that point the
    reader and the operator stop being the same person, and this becomes a real leak.
    """

    id: str
    title: str
    state: str = Field(
        description=(
            "'pending' while the queue still owns it, 'failed' once the retries ran out, "
            "'integrated' for the few minutes after it succeeded."
        )
    )
    attempts: int = Field(
        description="Processing attempts spent so far. 0 means it has not been picked up yet."
    )
    max_attempts: int = Field(
        description="Attempts before the pipeline gives up and the state becomes 'failed'."
    )
    next_attempt_at: datetime | None = Field(
        description=(
            "When the next attempt is due. Null means there is nothing scheduled: it is "
            "either being processed right now, or it is finished — see 'state'."
        )
    )
    last_error: str | None = Field(
        description=(
            "What the most recent attempt said, verbatim. Present while retrying as well "
            "as after failing, because the reason is the thing that says whether to wait, "
            "re-paste, or report it."
        )
    )
    created_at: datetime
    source_kind: str = Field(
        description=(
            "How this arrived: 'paste' for text pasted in, 'gmail' for a polled mailbox "
            "message. It decides what a person can do about a failure — a failed paste "
            "can be pasted again, and a failed mailbox message cannot, because the poll "
            "cursor has already moved past it."
        )
    )


class QueueHeartbeatResponse(BaseModel):
    """One queue, and when a worker was last draining it."""

    queue: str
    last_seen_at: datetime


class QueueReadinessResponse(BaseModel):
    """One queue's scaling signal: what is due, and how many workers could take it.

    Separate from :class:`QueueHeartbeatResponse` rather than folded into it, because the
    two lists answer different questions over different sets. A heartbeat exists only for a
    queue a worker has *run*; readiness exists for every queue, and the case it has to
    cover is precisely the one with no worker — a queue scaled to zero emits no gauge, so
    this route is the only place its backlog is visible (motet#78). Merging them would
    have meant widening ``last_seen_at`` to nullable, which is a breaking change to a
    shipped field for no gain.
    """

    queue: str
    ready: int = Field(
        description=(
            "Jobs on this queue that are ready and due now. Excludes anything backing off "
            "up the retry ladder or deferred because its serialization key was busy — "
            "neither is work a new worker could pick up. It also excludes work already "
            "`running`, so it is zero while a job is still going: a scaler needs a floor "
            "of one wherever a worker heartbeat is fresh."
        )
    )
    ready_keys: int = Field(
        description=(
            "How many of those jobs could be worked on at the same time: distinct "
            "serialization keys, plus one for each job that has no key. On a serialized "
            "queue (`integrate`, `poll`) this is the number of users with work waiting, "
            "which is the number of workers the queue can keep busy — invariant 6 holds "
            "the rest to one at a time. On an unserialized queue it equals `ready`."
        )
    )
    blocked_keys: int = Field(
        description=(
            "How many of those keys are already held by somebody right now, so the work "
            "is waiting on a worker that has it rather than on a worker that does not "
            "exist. Nonzero is the healthy case — a key is held whenever somebody is "
            "working it. This staying pinned while `ready` does not fall is the signal "
            "worth looking at: it is what a leaked or wedged advisory lock looks like."
        )
    )


class ProcessingStatusResponse(BaseModel):
    """Whether anything is actually draining the queues — motet#38's missing fact.

    The Processing panel used to tell the user "a worker takes it off the queue within a
    few seconds" whatever was true, because nothing in the system could say otherwise: a
    queued item looks the same whether a worker is chewing through a backlog or whether no
    worker has run since Tuesday. The client cannot derive it either — an item's age says
    how long it has waited, not whether anything is coming.

    ``worker_last_seen_at`` is null when no worker has *ever* run against this database,
    which is a different statement from "one ran a while ago" and reads differently on
    screen. Per-queue rows are here for the operator's version of the same question; the
    SPA reads the aggregate, because a Phase 1 deployment runs one process over all of
    them (``runner all``).

    ``now`` is here so the client never has to compare a database timestamp against a
    browser clock. It is a small field against a whole class of wrongness: a laptop
    resumed from sleep, or an unsynced VM, would otherwise report a perfectly healthy
    worker as gone and put a red banner over a pipeline that is running fine.
    """

    now: datetime = Field(
        description=(
            "The server's clock, at the moment this was answered. Every other timestamp "
            "the SPA ages — this one, an item's created_at — comes from the same clock, "
            "so a client that subtracts from this rather than from its own is immune to "
            "the two disagreeing."
        )
    )
    worker_last_seen_at: datetime | None = Field(
        description=(
            "When any worker last ran a drain pass, over any queue. Null means none ever "
            "has: nothing will happen to a queued item until one does."
        )
    )
    queues: list[QueueHeartbeatResponse] = Field(
        description="Per queue, most recently drained first. Absent queues have never run."
    )
    readiness: list[QueueReadinessResponse] = Field(
        description=(
            "Per queue, in pipeline order: how much work is due and how many workers "
            "could take it. Every queue is present, at zero when it has nothing."
        )
    )


class SourceSpanModel(BaseModel):
    """A half-open character range in a source item — what makes a claim checkable."""

    source_item_id: str
    start: int
    end: int


class NewsItemSourceRef(BaseModel):
    """A source item a news item is backed by, named so a list can show it."""

    id: str
    title: str


class NewsItemResponse(BaseModel):
    """A deduped story. Read state lives here, per invariant 5 — not per episode."""

    id: str
    title: str
    summary: str
    source_item_ids: list[str]
    sources: list[NewsItemSourceRef] = Field(
        description=(
            "The same source items as source_item_ids, with their titles, in position order."
        )
    )
    read: bool
    created_at: datetime


class ReadStateRequest(BaseModel):
    """Mark one news item read or unread.

    A body rather than two endpoints, because "unread" is a real thing a user wants: the
    backlog is the product's memory, and being unable to put something back is worse than
    never having marked it.
    """

    read: bool


class ClaimModel(BaseModel):
    """A reported assertion beside the span it came from (invariant 3).

    ``text`` is what gets spoken and may paraphrase; ``source_excerpt`` is the source text
    the span actually covers, resolved server-side. Both are sent because the episode
    screen shows them side by side — that display *is* the trust surface, and a client
    that had to fetch the source separately to render it would sometimes not bother.
    """

    text: str
    span: SourceSpanModel
    source_excerpt: str
    source_title: str


class SegmentResponse(BaseModel):
    news_item_id: str
    news_item_title: str
    text: str
    start_ms: int = Field(
        description=(
            "Where this segment starts in the episode audio. We own playback position "
            "(invariant 4); this never comes from a player."
        )
    )
    duration_ms: int
    claims: list[ClaimModel]


class EpisodeResponse(BaseModel):
    id: str
    title: str
    state: str = Field(description="pending -> scripting -> rendering -> ready, or failed.")
    duration_ms: int
    max_duration_ms: int
    audio_bytes: int | None
    audio_media_type: str | None
    last_error: str | None
    created_at: datetime
    published_at: datetime | None
    listened_through_ms: int = Field(
        description=(
            "How far the listener has been reported through this episode, in "
            "milliseconds. Served back so a device that has never played the episode can "
            "still resume where another one got to (invariant 4: the position is ours). "
            "Monotonic, so this is the furthest point reached rather than wherever a "
            "player happens to be parked right now."
        )
    )
    segments: list[SegmentResponse]


class CreateEpisodeRequest(BaseModel):
    """Phase 1 has manual episodes only: 'all unread', capped by duration."""

    title: str = Field(min_length=1, max_length=500)
    max_duration_ms: int = Field(gt=0)


class MarkListenedResponse(BaseModel):
    """The result of "I listened to this" — read state, synced (invariant 5)."""

    episode_id: str
    news_items_marked_read: int


class FeedInfoResponse(BaseModel):
    """The private feed URL, ready to paste into a podcast client.

    The token is returned in full rather than masked. It has to be: a feed URL is copied
    to a new device months after it was minted, and a secret the owner cannot read back is
    one that forces a rotation — which unsubscribes every client already using it.
    """

    url: str
    token: str


# --- Phase 2: connected sources ------------------------------------------------------


class SourceResponse(BaseModel):
    """A place source items come from — pasted text, or a connected mailbox."""

    id: str
    kind: str = Field(description="'paste' or 'gmail'.")
    name: str
    active: bool = Field(
        description="False means connected but paused: it is not polled, and nothing is lost."
    )
    connected: bool = Field(
        description=(
            "Whether a credential is stored for this source. Answered without decrypting "
            "anything — only workers can do that (invariant 8)."
        )
    )
    scopes: list[str] = Field(
        description="OAuth scopes actually granted, which may be more than were asked for."
    )
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


class ConnectSourceRequest(BaseModel):
    """Begin connecting a mailbox. Returns a URL for the user to visit."""

    provider: str = Field(
        default="gmail", description="Only 'gmail' in Phase 2. X bookmarks are not built."
    )
    name: str = Field(default="Gmail", min_length=1, max_length=200)
    query: str | None = Field(
        default=None,
        description=(
            "The provider's own search syntax, deciding which messages are newsletters. "
            "Defaults to Gmail's updates and promotions categories, which need no setup."
        ),
    )
    redirect_uri: str = Field(
        min_length=1,
        description=(
            "Where the provider sends the user back to. Supplied by the client rather "
            "than configured, because the SPA, a local dev server, and a future iOS app "
            "each have a different one."
        ),
    )


class ConnectSourceResponse(BaseModel):
    """Where to send the user, and the source the grant will attach to."""

    source_id: str
    authorization_url: str
    state: str = Field(
        description=(
            "The CSRF token for this authorization. Returned so a client can verify the "
            "callback it receives is the one it started."
        )
    )


class OAuthCallbackRequest(BaseModel):
    """What the provider redirected back with."""

    state: str = Field(min_length=1)
    code: str = Field(min_length=1)


# --- Phase 2: smart episodes ---------------------------------------------------------


class SmartRuleModel(BaseModel):
    """Filter, window, duration, ranking — how a smart episode chooses its stories.

    Duration is deliberately absent: it is ``max_duration_ms`` on the episode itself. Two
    copies of a cap is one too many, and the stale one is the one somebody would trust.
    """

    unread_only: bool = Field(
        default=True, description="Skip stories already read. Off for a 'catch me up' rule."
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="Only stories backed by these sources. Empty means every source.",
    )
    window_days: int = Field(
        default=2,
        ge=0,
        le=30,
        description="How far back to reach. 0 means no window, which is what manual does.",
    )
    ranking: str = Field(
        default="oldest_first",
        description=(
            "oldest_first (drains a backlog), newest_first (a morning briefing), or "
            "coverage (most independently reported first). All three are computed from "
            "the rows — ranking with a model is Phase 3."
        ),
    )
    max_items: int = Field(default=100, ge=1, le=100)


class CreateSmartEpisodeRequest(BaseModel):
    """An episode whose stories are selected by a rule rather than by 'all unread'."""

    title: str = Field(min_length=1, max_length=500)
    max_duration_ms: int = Field(gt=0)
    rule: SmartRuleModel = Field(default_factory=SmartRuleModel)


# --- Phase 2: read state from the audio side -----------------------------------------


class ListenProgressRequest(BaseModel):
    """How far into an episode the listener has got.

    Invariant 4: we own playback position, so this is a *report* from a client that we
    record, never a value read back out of a vendor SDK. Invariant 5 is what it does: a
    story whose segment has been passed is marked read, which is the same fact the backlog
    screen's toggle writes.

    The body of both ``PUT /v1/episodes/{id}/position`` and
    ``POST /v1/episodes/{id}/progress``, because they are one write. The name is the
    server's: ``spoken_through_ms`` is the voice session contract's word for a position
    that moves backwards when a listener seeks back, and this value deliberately does not.
    """

    listened_through_ms: int = Field(
        ge=0,
        description=(
            "Monotonic on the server: seeking backwards is reviewing, not un-listening, "
            "so a smaller value never lowers the recorded position or un-marks a story. "
            "Send the furthest point reached rather than the playhead — the response "
            "carries the value that was actually stored, which is what to resume from."
        ),
    )


class ListenProgressResponse(BaseModel):
    episode_id: str
    listened_through_ms: int = Field(description="The position after applying monotonicity.")
    news_items_marked_read: int


# --- Phase 2: highlights -------------------------------------------------------------


class HighlightResponse(BaseModel):
    """A saved passage, anchored to the span of source text it quotes.

    The anchor is the source span and nothing else — claims are rewritten on every script
    retry and audio offsets move on every re-render, while a source item's text never
    changes. ``episode_id`` and ``anchor_ms`` say where the listener was when they saved
    it: provenance, not the anchor.
    """

    id: str
    news_item_id: str
    source_item_id: str
    span: SourceSpanModel
    quote: str = Field(
        description=(
            "What the source actually says at that span, read from the source item rather "
            "than taken from the caller — so a model calling save_highlight cannot write "
            "its paraphrase in and have it look verbatim."
        )
    )
    note: str | None
    episode_id: str | None
    anchor_ms: int | None
    created_at: datetime


class SaveHighlightRequest(BaseModel):
    """Save a passage. The platform tool `save_highlight` posts exactly this."""

    news_item_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=2000)
    episode_id: str | None = Field(
        default=None, description="Where the user was listening. Provenance, not the anchor."
    )
    anchor_ms: int | None = Field(default=None, ge=0)


# --- signing in ----------------------------------------------------------------------


class StartLoginRequest(BaseModel):
    """Begin a Google sign-in. Answered with a URL for the browser to visit."""

    redirect_uri: str = Field(
        min_length=1,
        description=(
            "Where Google sends the browser back to — this deployment's SPA origin plus "
            "/oauth/callback. Supplied by the client for the same reason connecting a "
            "mailbox supplies it: one bundle serves three environments, each with its own "
            "origin. It must be registered on the OAuth client, and when "
            "MOTET_APP_BASE_URL is set the API additionally requires it to match."
        ),
    )


class StartLoginResponse(BaseModel):
    """Where to send the browser, and the state that identifies this sign-in."""

    authorization_url: str
    state: str = Field(
        description=(
            "The CSRF token for this sign-in. Returned so a client can verify the "
            "callback it receives is the one it started, and prefixed 'login.' so the "
            "single /oauth/callback path can tell a sign-in from a mailbox connection."
        )
    )


class CompleteLoginRequest(BaseModel):
    """What Google redirected back with."""

    state: str = Field(min_length=1)
    code: str = Field(min_length=1)


class LoginResponse(BaseModel):
    """A session, and the token that presents it.

    ``token`` is returned exactly once, here — the API stores only its hash, so it cannot
    be read back. A client that loses it signs in again.
    """

    token: str = Field(description="Send as 'Authorization: Bearer <token>', like the API token.")
    email: str = Field(description="The Google account that signed in.")
    expires_at: datetime


class SessionResponse(BaseModel):
    """Who the caller is, as far as this API is concerned.

    Answers for the shared API token too, which is what lets the SPA show "signed in as
    …" or "using an API token" without guessing from what it has in storage.
    """

    how: str = Field(
        description=(
            "'session' for a signed-in browser, 'token' for the shared API token, 'open' "
            "when MOTET_API_TOKEN is unset and this deployment has no lock on it at all."
        )
    )
    email: str | None = None
    expires_at: datetime | None = None
    login_configured: bool = Field(
        description="Whether this deployment can complete a Google sign-in at all."
    )
    admin: bool = Field(
        description=(
            "Whether this caller may read /v1/admin/*: a signed-in session whose address is "
            "on MOTET_ADMIN_EMAILS. Always false for the shared API token and for an open "
            "deployment, and for everybody when MOTET_ADMIN_EMAILS is unset."
        )
    )


class RevokedResponse(BaseModel):
    """How many sessions a revoke-everywhere took out."""

    revoked: int = Field(
        description="Sessions destroyed, including the caller's own if it had one."
    )


# --- admin overview ------------------------------------------------------------------


class AdminSourceItemCounts(BaseModel):
    pending: int
    integrated: int
    failed: int


class AdminNewsItemCounts(BaseModel):
    unread: int
    read: int


class AdminEpisodeCounts(BaseModel):
    pending: int
    scripting: int
    rendering: int
    ready: int
    failed: int


class AdminJobCounts(BaseModel):
    ready: int
    running: int
    done: int
    failed: int


class AdminUserResponse(BaseModel):
    """One user's counts per state, across every table that carries a `user_id`."""

    user_id: str
    email: str | None
    source_items: AdminSourceItemCounts
    news_items: AdminNewsItemCounts
    episodes: AdminEpisodeCounts
    jobs: AdminJobCounts


class AdminQueueResponse(BaseModel):
    """One queue's counts per job state, plus the two liveness facts an operator wants."""

    queue: str
    ready: int
    running: int
    done: int
    failed: int
    oldest_ready_age_s: float | None = Field(
        description="Seconds since the oldest `ready` job on this queue was created; null if none."
    )
    last_heartbeat_at: datetime | None = Field(
        description="When a worker last drained this queue; null if none ever has."
    )


class AdminJobResponse(BaseModel):
    """One job row, with its payload resolved to a user and a domain subject."""

    id: int
    queue: str
    state: str
    attempts: int
    user_id: str | None = Field(
        description=(
            "The user the job's subject belongs to, resolved from the payload; null if "
            "unresolvable."
        )
    )
    subject: str | None = Field(
        description="The domain id the job is about: a source item, an episode, or a source."
    )
    last_error: str | None
    run_at: datetime
    created_at: datetime
    updated_at: datetime
    locked_at: datetime | None


class AdminOverviewResponse(BaseModel):
    """The whole deployment at a glance, across every user. Admins only.

    Aggregates are always for everyone; only `jobs` is paged, and filtered when a
    `user_id` is asked for. Every user and every pipeline queue is present, at zero when
    empty.
    """

    generated_at: datetime
    users: list[AdminUserResponse]
    queues: list[AdminQueueResponse]
    jobs: list[AdminJobResponse] = Field(
        description=(
            "One page of jobs in any state, newest first (by id). `limit` long at most; "
            "`user_id` narrows it to jobs resolved to that user."
        )
    )
    jobs_next_before: int | None = Field(
        description=("Pass as `before` for the next, older page; null when this page is the last.")
    )
