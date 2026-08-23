"""The Motet HTTP API.

**Scaffold.** Every route below the health check declares its contract and raises 501. The
point of Phase 1's factory work is that the seam between the API and the SPA exists and is
enforced by CI — not that it does anything yet. Implementing a route means replacing its
body; the shape, the models, and the generated client are already in place.

Routes are versioned under ``/v1`` so the contract can move without breaking a shipped
client — invariant 1 means clients only ever speak *this* protocol, so it is the one that
has to stay stable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Response, status
from motet_inference.llm import load_config as load_llm_config

from . import obs
from .schemas import (
    CreateEpisodeRequest,
    EpisodeResponse,
    HealthResponse,
    NewsItemResponse,
    PasteRequest,
    SourceItemResponse,
)

NOT_BUILT_YET = "Not implemented: Phase 1 scaffold. See AGENTS.md."


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
    config = load_llm_config()
    obs.logger.info("llm: %s", config.describe())
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


def _not_implemented() -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=NOT_BUILT_YET)


@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz() -> HealthResponse:
    """Liveness, plus whether telemetry is actually wired.

    The telemetry flags are not decoration. Exporters no-op silently when unconfigured, so
    without this an unmonitored process is indistinguishable from a quiet one.
    """
    current = obs.status()
    return HealthResponse(
        status="ok",
        service=current.service_name,
        telemetry_configured=current.otlp_configured,
        errors_configured=current.errors_configured,
    )


@app.post(
    "/v1/sources/paste",
    response_model=SourceItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["ingestion"],
)
def paste_source(body: PasteRequest) -> SourceItemResponse:
    """Ingest pasted text as a source item.

    Enqueues rather than processes: ingestion is serialized per user (invariant 6), so the
    work belongs to a worker draining the queue, never to the request thread.
    """
    raise _not_implemented()


@app.get("/v1/news-items", response_model=list[NewsItemResponse], tags=["backlog"])
def list_news_items() -> list[NewsItemResponse]:
    """The backlog: deduped news items with their read state (invariant 5)."""
    raise _not_implemented()


@app.post(
    "/v1/episodes",
    response_model=EpisodeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["episodes"],
)
def create_episode(body: CreateEpisodeRequest) -> EpisodeResponse:
    """Assemble a manual episode from unread news items, capped by duration.

    Scripting, grounding validation, and TTS happen on the queue afterwards. Nothing is
    synthesized until grounding passes (invariant 3).
    """
    raise _not_implemented()


@app.get("/v1/episodes/{episode_id}", response_model=EpisodeResponse, tags=["episodes"])
def get_episode(episode_id: Annotated[str, Path()]) -> EpisodeResponse:
    """An episode with its transcript — each claim beside the span it came from."""
    raise _not_implemented()


@app.get(
    "/feed.xml",
    tags=["feed"],
    response_class=Response,
    responses={200: {"content": {"application/rss+xml": {}}, "description": "The RSS feed"}},
)
def rss_feed() -> Response:
    """The private, authenticated RSS feed Phase 1 ships instead of a player.

    RSS buys background audio, offline, lockscreen, CarPlay, and speed control with zero
    iOS code. Audio is served from GCS behind signed URLs, so this document carries links,
    never bytes.
    """
    raise _not_implemented()
