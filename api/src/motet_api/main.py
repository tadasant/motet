"""The Motet HTTP API.

Routes are versioned under ``/v1`` so the contract can move without breaking a shipped
client — invariant 1 means clients only ever speak *this* protocol, so it is the one that
has to stay stable. The feed is deliberately outside ``/v1``: ``/feed.xml`` is a URL a
human pastes into a podcast client, and a version number in it would be a version number
in something that has to keep working for years.

**The API never runs inference.** It writes rows and enqueues jobs; workers call models.
That is why it validates LLM *configuration* at startup but never resolves the key —
mounting the one vendor secret in the system into the internet-facing service would widen
the blast radius for no functional gain.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from motet_db import StoredEpisode, StoredNewsItem, repo
from motet_inference.llm import load_config as load_llm_config
from motet_storage import ObjectStore, StorageError
from motet_workers import enqueue_episode, enqueue_paste

from . import obs
from .config import APP_BASE_URL_ENV, Settings
from .deps import (
    connection,
    public_base_url,
    require_api_token,
    require_feed_token,
    settings,
    store,
)
from .feed import FeedMetadata, feed_url, render_feed
from .schemas import (
    ClaimModel,
    CreateEpisodeRequest,
    EpisodeResponse,
    FeedInfoResponse,
    HealthResponse,
    MarkListenedResponse,
    NewsItemResponse,
    PasteRequest,
    ReadStateRequest,
    SegmentResponse,
    SourceItemResponse,
    SourceSpanModel,
)

logger = logging.getLogger("motet.api")

Conn = Annotated[psycopg.Connection[Any], Depends(connection)]
User = Annotated[str, Depends(require_api_token)]
FeedUser = Annotated[str, Depends(require_feed_token)]
Config = Annotated[Settings, Depends(settings)]
Store = Annotated[ObjectStore, Depends(store)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Refuse to serve at all rather than serve a request we cannot fulfil.

    An unknown model slug or a nonsense effort stops the process here, where Cloud Run
    reports a failed revision and never shifts traffic to it. Discovering the same fact on
    the first inference request means a 500 an hour after the deploy, with nothing tying it
    to the change that caused it.

    **Config only — deliberately not the credential.** ``validate_startup`` also resolves
    the API key, and the worker entry point calls it for exactly that reason. Requiring it
    here would mean mounting the one vendor secret in the system into the *internet-facing*
    service, which in Phase 1 never calls a model at all: inference runs in workers. The
    day the API calls a model directly, this becomes ``validate_startup`` and the key
    becomes its business.
    """
    # First, so that everything below is logged through the configured handler rather
    # than through whatever `logging` falls back to.
    obs.configure()
    config = load_llm_config()
    obs.logger.info("llm: %s", config.describe())
    current = Settings.from_env()
    if not current.cors_origins:
        obs.logger.warning(
            "%s is unset: no browser origin is allowed to call /v1. The SPA is served "
            "from a different hostname than this API in every deployed environment, so "
            "this means the web app cannot reach it at all.",
            APP_BASE_URL_ENV,
        )
    if not current.authenticated:
        obs.logger.warning(
            "MOTET_API_TOKEN is unset: /v1 is open to anyone who can reach this process. "
            "Fine on a laptop; on a deployed environment it means anyone can ingest text "
            "and spend inference budget."
        )
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Motet API",
    version="0.1.0",
    description=(
        "Motet turns a reading backlog into an interactive podcast.\n\n"
        "This document is generated from the FastAPI app and committed as `openapi.yaml`; "
        "the TypeScript client is generated from it in turn. Do not hand-edit either."
    ),
)

# CORS, because the SPA is on `app.` and this API is on `api.` — two origins, so every
# call the web app makes is cross-origin and a browser blocks it by default. Without this
# the SPA loads, renders, and fails every request with an opaque network error that says
# nothing about the cause.
#
# Read once at import rather than per request: an origin policy that could change under a
# running process would be a policy nobody could reason about, and Cloud Run gives a new
# revision for an environment change anyway.
_cors_origins = Settings.from_env().cors_origins
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        # Exact origins, never `*`. See Settings.cors_origins.
        allow_origins=_cors_origins,
        # The SPA sends `Authorization`, which makes every request preflighted and
        # credentialed. Both halves have to be allowed or the browser drops the header
        # and the API answers 401 to a request that looked correct in the network tab.
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz(config: Config) -> HealthResponse:
    """Liveness, plus whether telemetry and authentication are actually wired.

    The flags are not decoration. Exporters no-op silently when unconfigured, so without
    this an unmonitored process is indistinguishable from a quiet one — and an
    unauthenticated deployment is indistinguishable from a working one until the bill
    arrives.
    """
    current = obs.status()
    return HealthResponse(
        status="ok",
        service=current.service_name,
        telemetry_configured=current.otlp_configured,
        errors_configured=current.errors_configured,
        authenticated=config.authenticated,
        inference_mode=config.inference_mode,
    )


@app.post(
    "/v1/sources/paste",
    response_model=SourceItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["ingestion"],
)
def paste_source(body: PasteRequest, conn: Conn, user_id: User) -> SourceItemResponse:
    """Ingest pasted text as a source item.

    Enqueues rather than processes: ingestion is serialized per user (invariant 6), so the
    work belongs to a worker draining the queue, never to the request thread. The row and
    the job are written in the same transaction — with two systems there would always be a
    window where the source item exists and nothing will ever pick it up.
    """
    stored = enqueue_paste(conn, user_id=user_id, title=body.title.strip(), text=body.text)
    return SourceItemResponse(id=stored.id, title=stored.title, state=stored.state.value)


@app.get("/v1/news-items", response_model=list[NewsItemResponse], tags=["backlog"])
def list_news_items(conn: Conn, user_id: User) -> list[NewsItemResponse]:
    """The backlog: deduped news items with their read state (invariant 5)."""
    return [_news_item(item) for item in repo.list_news_items(conn, user_id)]


@app.post("/v1/news-items/{news_item_id}/read", response_model=NewsItemResponse, tags=["backlog"])
def set_news_item_read(
    body: ReadStateRequest,
    conn: Conn,
    user_id: User,
    news_item_id: Annotated[str, Path()],
) -> NewsItemResponse:
    """Mark a news item read or unread.

    The same write that "I listened to this episode" performs, which is what invariant 5
    means in practice: one fact, one column, two ways of reaching it.
    """
    updated = repo.set_news_item_read(conn, user_id=user_id, item_id=news_item_id, read=body.read)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such news item.")
    return _news_item(updated)


@app.post(
    "/v1/episodes",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_episode(body: CreateEpisodeRequest, conn: Conn, user_id: User) -> EpisodeResponse:
    """Assemble a manual episode from unread news items, capped by duration.

    Returns immediately, in ``pending``. Assembly, scripting, grounding validation, and TTS
    happen on the queue afterwards, and nothing is synthesized until grounding passes
    (invariant 3) — so the episode a client polls for moves through states rather than
    appearing finished.
    """
    episode_id = enqueue_episode(
        conn, user_id=user_id, title=body.title.strip(), max_duration_ms=body.max_duration_ms
    )
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    assert episode is not None
    return _episode(conn, episode)


@app.get("/v1/episodes", response_model=list[EpisodeResponse], tags=["episodes"])
def list_episodes(conn: Conn, user_id: User) -> list[EpisodeResponse]:
    """Every episode, newest first, whatever state it is in."""
    return [_episode(conn, episode) for episode in repo.list_episodes(conn, user_id)]


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeResponse, tags=["episodes"])
def get_episode(conn: Conn, user_id: User, episode_id: Annotated[str, Path()]) -> EpisodeResponse:
    """An episode with its transcript — each claim beside the span it came from."""
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.")
    return _episode(conn, episode)


@app.post(
    "/v1/episodes/{episode_id}/listened",
    response_model=MarkListenedResponse,
    tags=["episodes"],
)
def mark_episode_listened(
    conn: Conn, user_id: User, episode_id: Annotated[str, Path()]
) -> MarkListenedResponse:
    """Mark every news item in this episode read.

    Phase 1's stand-in for playback tracking: RSS gives background audio and CarPlay for
    free, and takes away any way for a client to report where the listener got to. Phase
    2's iOS app reports ``spoken_through_ms`` and this becomes automatic — but the fact it
    writes is the same one, on the same column, which is why swapping the trigger later
    changes nothing about read state.
    """
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such episode.")
    marked = repo.mark_news_items_read(
        conn, user_id=user_id, item_ids=[s.news_item_id for s in episode.segments]
    )
    return MarkListenedResponse(episode_id=episode.id, news_items_marked_read=marked)


@app.get("/v1/feed", response_model=FeedInfoResponse, tags=["feed"])
def get_feed_info(request: Request, conn: Conn, user_id: User, config: Config) -> FeedInfoResponse:
    """The private feed URL, minting a token on first ask."""
    token = repo.ensure_feed_token(conn, user_id)
    base = public_base_url(config, str(request.base_url))
    return FeedInfoResponse(url=feed_url(base, token), token=token)


@app.post("/v1/feed/rotate", response_model=FeedInfoResponse, tags=["feed"])
def rotate_feed(request: Request, conn: Conn, user_id: User, config: Config) -> FeedInfoResponse:
    """Revoke the current feed URL and mint a new one.

    This unsubscribes every client using the old URL, which is the point — it is the
    answer to a leaked feed link, and there is no other way to take one back.
    """
    token = repo.rotate_feed_token(conn, user_id)
    base = public_base_url(config, str(request.base_url))
    return FeedInfoResponse(url=feed_url(base, token), token=token)


@app.get(
    "/feed.xml",
    tags=["feed"],
    response_class=Response,
    responses={200: {"content": {"application/rss+xml": {}}, "description": "The RSS feed"}},
)
def rss_feed(request: Request, conn: Conn, user_id: FeedUser, config: Config) -> Response:
    """The private, authenticated RSS feed Phase 1 ships instead of a player.

    RSS buys background audio, offline, lockscreen, CarPlay, and speed control with zero
    iOS code. Audio is served from object storage behind signed URLs, so this document
    carries links, never bytes.
    """
    token = repo.active_feed_token(conn, user_id)
    assert token is not None  # the dependency resolved this request's token from this row
    base = public_base_url(config, str(request.base_url))
    body = render_feed(
        FeedMetadata(
            title=config.feed_title,
            description=config.feed_description,
            author=config.feed_author,
            base_url=base,
            token=token,
        ),
        repo.list_published_episodes(conn, user_id),
    )
    return Response(content=body, media_type="application/rss+xml")


@app.get(
    "/v1/episodes/{episode_id}/audio",
    tags=["feed"],
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "The episode audio"},
        307: {"description": "Redirect to a time-limited signed URL"},
    },
)
def episode_audio(
    conn: Conn,
    user_id: FeedUser,
    blobs: Store,
    episode_id: Annotated[str, Path()],
) -> Response:
    """Serve an episode's audio, or redirect to a signed URL for it.

    Which of the two depends on the storage backend, and the *store* decides rather than
    this route: a backend that can mint a signed URL returns one, and one that cannot
    returns ``None``. A podcast client cannot tell the difference — it follows the
    redirect — so the enclosure URL in the feed is stable across both, and a signed URL's
    expiry never ends up cached inside a feed document.
    """
    episode = repo.get_episode(conn, episode_id, user_id=user_id)
    if episode is None or not episode.has_audio or episode.audio_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This episode has no audio yet.")

    signed = blobs.signed_url(episode.audio_key)
    if signed is not None:
        return RedirectResponse(signed, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    try:
        data = blobs.get(episode.audio_key)
    except StorageError as exc:
        logger.error("episode %s audio is missing from storage: %s", episode.id, exc)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This episode's audio is no longer available."
        ) from exc
    # Deliberately no `Accept-Ranges: bytes`. Podcast clients do range-request large files,
    # but this branch serves the whole body and ignores `Range` — advertising support we do
    # not have would tell a resuming client it had resumed when it had started over. The
    # deployed backend hands out a signed URL above and gets real range support from object
    # storage; this path is dev and CI only.
    return Response(
        content=data,
        media_type=episode.audio_media_type or "audio/mpeg",
        headers={"Content-Length": str(len(data))},
    )


def _news_item(item: StoredNewsItem) -> NewsItemResponse:
    return NewsItemResponse(
        id=item.id,
        title=item.title,
        summary=item.summary,
        source_item_ids=list(item.source_item_ids),
        read=item.read,
        created_at=item.created_at,
    )


def _episode(conn: psycopg.Connection[Any], episode: StoredEpisode) -> EpisodeResponse:
    """Build the episode view, resolving every claim's span to the text it cites.

    The resolution happens here rather than in the client because it is the whole point of
    the screen: a claim shown next to the sentence it came from is the product's argument
    that it is not making things up. A client that had to fetch sources separately would
    sometimes skip it, and the argument would quietly stop being made.
    """
    source_ids = {claim.source_item_id for segment in episode.segments for claim in segment.claims}
    sources = repo.load_source_items(conn, sorted(source_ids))
    news_titles = {
        item_id: item.title
        for item_id, item in repo.load_news_items(
            conn, [segment.news_item_id for segment in episode.segments]
        ).items()
    }

    segments = []
    for segment in episode.segments:
        claims = []
        for claim in segment.claims:
            source = sources.get(claim.source_item_id)
            excerpt = source.text[claim.span_start : claim.span_end] if source is not None else ""
            claims.append(
                ClaimModel(
                    text=claim.text,
                    span=SourceSpanModel(
                        source_item_id=claim.source_item_id,
                        start=claim.span_start,
                        end=claim.span_end,
                    ),
                    source_excerpt=excerpt,
                    source_title=source.title if source is not None else "(source removed)",
                )
            )
        segments.append(
            SegmentResponse(
                news_item_id=segment.news_item_id,
                news_item_title=news_titles.get(segment.news_item_id, "(story removed)"),
                text=segment.text,
                start_ms=segment.start_ms,
                duration_ms=segment.duration_ms,
                claims=claims,
            )
        )

    return EpisodeResponse(
        id=episode.id,
        title=episode.title,
        state=episode.state.value,
        duration_ms=episode.duration_ms,
        max_duration_ms=episode.max_duration_ms,
        audio_bytes=episode.audio_bytes,
        audio_media_type=episode.audio_media_type,
        last_error=episode.last_error,
        created_at=episode.created_at,
        published_at=episode.published_at,
        segments=segments,
    )
