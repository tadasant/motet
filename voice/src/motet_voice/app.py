"""The voice service as an HTTP + WebSocket app, built to run on Cloud Run.

```
POST /v1/voice/sessions                      StartSession(...) -> session_token
WS   /v1/voice/sessions/{session_id}/stream  audio in; transcripts, tool calls,
                                             audio chunks and interrupted_at out
GET  /internal/health                        what is wired, and what is dormant
```

**Three Cloud Run facts shape this file**, and each of them is a decision rather than an
accident:

1. **No sticky sessions.** The socket may land on a different instance than the one that
   minted the token, so there is no server-side session record. The client re-sends the
   config on the socket and the token's digest proves it is the same config StartSession
   approved. See :mod:`motet_voice.tokens`.
2. **No persistent box.** An instance can go away between two turns. A session holds nothing
   that is not either in the token or already sent to the client.
3. **WebSockets are supported but bounded.** A request — including a socket — has a maximum
   duration, so the client must be able to reconnect, which the stateless token makes free.

**A client never speaks a vendor protocol here** (invariant 1): the events below are ours,
and swapping the arm underneath changes none of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError

from . import obs, tokens
from .config import START_SESSION_TOKEN_ENV, VoiceSettings, load_settings
from .contract import (
    PLATFORM_TOOLS,
    ErrorEvent,
    SessionEvent,
    SessionStateEvent,
    StartSessionRequest,
    StartSessionResponse,
)
from .grounding import ConversationGroundingChecker, build_grounding_checker
from .realtime import RealtimeArm, build_arm
from .session import VoiceSession
from .tools import HttpToolTransport, ToolRegistry, ToolTransport, build_platform_tools

logger = logging.getLogger("motet.voice.app")

WEBSOCKET_PATH = "/v1/voice/sessions/{session_id}/stream"

#: Health, and deliberately **not** ``/healthz``: Cloud Run's frontend answers that path
#: with its own 404 before the request reaches the container, so a health endpoint served
#: there is unreadable from everywhere health is actually checked. This service runs on
#: the same platform as the API, so it takes the same path. See ``motet_api.main`` for the
#: full account, and motet#16.
HEALTH_PATH = "/internal/health"

#: Namespaces the platform claims. Prefix-matched, and guarded by a test — see
#: ``voice/tests/test_app.py``.
#:
#: Deliberately a second copy of ``motet_api.main.PLATFORM_RESERVED_PATHS`` rather than an
#: import: invariant 2 keeps this service free of Motet-side imports, and a shared package
#: for one tuple would be a dependency edge bought for nothing. **Keep the two in step** —
#: a path added there belongs here too, and vice versa.
PLATFORM_RESERVED_PATHS = ("/healthz", "/_ah")

#: How long an accepted socket may go without authenticating. Short: a client that
#: has just been handed a token sends it immediately.
AUTHENTICATE_TIMEOUT_SECONDS = 10.0

#: How long the socket waits, at close, for events already queued to go out. Bounded
#: because a client that has walked out of signal will never drain, and unbounded patience
#: there would hold a Cloud Run concurrency slot for nothing.
FLUSH_TIMEOUT_SECONDS = 5.0


class HealthResponse(BaseModel):
    status: str
    service: str
    telemetry_configured: bool
    errors_configured: bool
    inference_mode: str
    arm: str
    arm_conversational: bool
    arm_dormant_reason: str
    session_secret_configured: bool
    #: ``False`` means anyone who can reach this service can mint a session. Reported for
    #: the same reason the API reports its own: an open deployment is indistinguishable
    #: from a working one until something goes wrong.
    start_session_authenticated: bool
    #: Whether this process installed an exporter, as opposed to merely having the
    #: variables set. The two were different for months on the API, which is how a service
    #: looks monitored and emits nothing — and the advisory grounding counters are only
    #: worth anything if this is true.
    telemetry_exporting: bool
    #: Which advisory grounding checker is running on the conversational reply path, and
    #: whether it gates audio. It never does — motet#10 — and the field says so out loud
    #: rather than leaving a reader of ``/internal/health`` to assume invariant 3's hard
    #: narration gate applies here too.
    grounding_checker: str
    grounding_advisory: bool
    tools: list[dict[str, Any]]


class VoiceApp:
    """Process-wide state: the settings, the arm, and the tool transport.

    Held on the app rather than in module globals so a test can build a second one with
    different settings, which is what the arm-swap tests do.
    """

    def __init__(
        self,
        settings: VoiceSettings | None = None,
        *,
        arm: RealtimeArm | None = None,
        transport: ToolTransport | None = None,
        grounding: ConversationGroundingChecker | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.arm = arm or build_arm(self.settings)
        self._explicit_transport = transport is not None
        self.transport = transport or self._build_transport()
        # Process-wide because it is stateless and free: unlike the VAD, which carries one
        # stream's noise floor and must be per-session, the checker holds nothing.
        self.grounding = grounding or build_grounding_checker()

    def _build_transport(self) -> ToolTransport | None:
        if not self.settings.api_base_url:
            return None
        return HttpToolTransport(self.settings.api_base_url, self.settings.api_token)

    def registry(self, request: StartSessionRequest) -> ToolRegistry:
        """The tools this session asked for, of the ones the platform offers.

        Resolved at StartSession so the persona's prompt and the session's actual
        capabilities cannot disagree — see :class:`~motet_voice.tools.spec.ToolRegistry`.
        """
        available = build_platform_tools(
            self.settings,
            transport=self.transport,
            defaults={binding.name: binding.defaults for binding in request.tools},
        )
        wanted = [binding.name for binding in request.tools] or list(PLATFORM_TOOLS)
        return ToolRegistry({name: available[name] for name in wanted if name in available})

    async def aclose(self) -> None:
        await self.arm.aclose()
        if self.transport is not None and not self._explicit_transport:
            await self.transport.aclose()


def create_app(
    settings: VoiceSettings | None = None,
    *,
    arm: RealtimeArm | None = None,
    transport: ToolTransport | None = None,
    grounding: ConversationGroundingChecker | None = None,
) -> FastAPI:
    """Build the ASGI app. Injectable so tests never touch a network or a vendor."""
    state = VoiceApp(settings, arm=arm, transport=transport, grounding=grounding)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Say what is wired at boot.

        Deliberately **not** a startup crash on a missing vendor key, unlike the API's
        lifespan. The reason is specific: this service's most valuable function tonight is
        turn detection, which needs no credential at all. Refusing to boot without one
        would take the measurement offline to protect a leg nobody is using.
        """
        obs.configure()
        logger.info("voice: %s", state.settings.describe())
        capabilities = state.arm.capabilities()
        if capabilities.dormant_reason:
            logger.warning("arm %s is dormant: %s", capabilities.name, capabilities.dormant_reason)
        try:
            yield
        finally:
            # In a `finally` because the flush is the half that matters: an instance that
            # goes down badly is exactly the one whose last batch of verdicts nobody would
            # think to go looking for. `obs.shutdown` after the arm, so anything recorded
            # on the way down is in the batch that goes.
            await state.aclose()
            obs.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        title="Motet Voice",
        version="0.1.0",
        description=(
            "The voice session contract. Clients speak this and never a vendor protocol; "
            "the service holds no database credential and reaches Motet only through tools."
        ),
    )
    # Request spans and HTTP server metrics. Here rather than in the lifespan because
    # instrumenting adds middleware and Starlette refuses that once the stack is built —
    # which it is by the time a lifespan event arrives. Safe before any provider exists:
    # the middleware holds a proxy tracer that resolves when `obs.configure` runs.
    obs.instrument(app)
    app.state.voice = state

    @app.get(HEALTH_PATH, response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness, and — more usefully — what is dormant and why.

        The dormancy fields are the point. An exporter that no-ops and an arm with no
        credential both look exactly like a healthy quiet service from the outside.
        """
        current = obs.status()
        capabilities = state.arm.capabilities()
        registry = state.registry(
            StartSessionRequest.model_validate(
                {"persona": {"name": "probe", "instructions": "probe"}}
            )
        )
        return HealthResponse(
            status="ok",
            service=current.service_name,
            telemetry_configured=current.otlp_configured,
            errors_configured=current.errors_configured,
            telemetry_exporting=current.exporting,
            grounding_checker=state.grounding.name,
            # Stated as a constant rather than read from anything, because there is nothing
            # to read: the conversational path has no gate to switch on. Invariant 3's hard
            # gate lives on the narration path, in the pipeline, and is not this service's.
            grounding_advisory=True,
            inference_mode=state.settings.inference_mode,
            arm=capabilities.name,
            arm_conversational=capabilities.conversational,
            arm_dormant_reason=capabilities.dormant_reason,
            session_secret_configured=state.settings.session_secret_provided,
            start_session_authenticated=state.settings.start_session_token is not None,
            tools=registry.describe(),
        )

    @app.post(
        "/v1/voice/sessions",
        response_model=StartSessionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["session"],
    )
    def start_session(
        request: StartSessionRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StartSessionResponse:
        """``StartSession(persona, tools, mcp_servers, context, turn_policy) -> token``."""
        _authorize_start_session(state.settings, authorization)
        unknown = [binding.name for binding in request.tools if binding.name not in PLATFORM_TOOLS]
        if unknown:
            # Rejected here rather than at call time: a persona told it can do something it
            # cannot will spend the session apologizing for a tool that never existed.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown tool(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(PLATFORM_TOOLS)}",
            )
        if request.mcp_servers:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "no MCP servers are configured for voice sessions yet; the field is part of "
                "the contract but this deployment resolves no slugs",
            )

        session_id = uuid.uuid4().hex
        token, expires_at = tokens.mint(
            session_id=session_id,
            secret=state.settings.session_secret,
            ttl_seconds=state.settings.session_ttl_seconds,
            digest=tokens.config_digest(request.model_dump(mode="json")),
        )
        capabilities = state.arm.capabilities()
        return StartSessionResponse(
            session_id=session_id,
            session_token=token,
            expires_at=str(expires_at),
            websocket_path=WEBSOCKET_PATH.format(session_id=session_id),
            arm=capabilities.name,
            conversational=capabilities.conversational,
        )

    @app.websocket(WEBSOCKET_PATH)
    async def stream(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        session = await _authenticate(websocket, state, session_id)
        if session is None:
            return
        try:
            await _pump(websocket, session)
        except WebSocketDisconnect:
            logger.info("client disconnected: %s", session_id)
        finally:
            await session.aclose()

    return app


def _authorize_start_session(settings: VoiceSettings, authorization: str | None) -> None:
    """Guard the one route that mints sessions.

    **A session is a capability, not a document.** Its tools call Motet's API carrying *this
    service's* bearer, so an open ``StartSession`` is not merely a way to spend inference
    budget — it is a confused deputy with a read path into the corpus, reachable by anyone
    who can route to the service.

    Unset means open, exactly as ``MOTET_API_TOKEN`` does on the API, and for the same
    reason: a laptop should need no setup. It warns on every request rather than only at
    boot, because a warning nobody sees after the first minute of uptime is not a warning.
    """
    if settings.start_session_token is None:
        logger.warning(
            "minting a voice session for an unauthenticated caller: %s is unset",
            START_SESSION_TOKEN_ENV,
        )
        return

    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not secrets.compare_digest(presented, settings.start_session_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer token is required to start a voice session.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _authenticate(
    websocket: WebSocket, state: VoiceApp, session_id: str
) -> VoiceSession | None:
    """First frame must be ``{"type": "authenticate", "token": ..., "config": {...}}``.

    The config comes back over the socket because there is nowhere to have kept it — see the
    module docstring. Its digest is checked against the one signed into the token, so a valid
    token cannot be paired with a different tool list.
    """
    try:
        # Bounded: an accepted socket that never authenticates holds a Cloud Run concurrency
        # slot until the platform's own request timeout, which is minutes. Anyone can open
        # one, so the cheapest denial-of-service against this service is silence.
        async with asyncio.timeout(AUTHENTICATE_TIMEOUT_SECONDS):
            raw = await websocket.receive_text()
        payload = json.loads(raw)
    except TimeoutError:
        await _close(websocket, "no authenticate frame arrived in time")
        return None
    except (WebSocketDisconnect, ValueError):
        await _close(websocket, "malformed authentication frame")
        return None

    if not isinstance(payload, dict) or payload.get("type") != "authenticate":
        await _close(websocket, "first frame must be an authenticate message")
        return None

    try:
        config = StartSessionRequest.model_validate(payload.get("config") or {})
    except ValidationError as exc:
        await _close(websocket, f"invalid session config: {exc.error_count()} problem(s)")
        return None

    try:
        claims = tokens.verify(
            str(payload.get("token", "")),
            secret=state.settings.session_secret,
            digest=tokens.config_digest(config.model_dump(mode="json")),
        )
    except tokens.SessionTokenError as exc:
        await _close(websocket, str(exc))
        return None

    if claims.session_id != session_id:
        await _close(websocket, "session token does not match this session id")
        return None

    session = VoiceSession.create(
        session_id=session_id,
        config=config,
        arm=state.arm,
        tools=state.registry(config),
        grounding=state.grounding,
    )
    await _send(websocket, session.ready())
    return session


async def _pump(websocket: WebSocket, session: VoiceSession) -> None:
    """The session loop: audio frames in, events out.

    Binary frames are listener audio. Text frames are control messages. Anything
    unrecognized gets an ``error`` event rather than a closed socket — a client that sends
    one bad message should not lose a walk.

    **Every outbound event goes through ``session.outbox`` and one sender task**, rather
    than being written here. Grounding is advisory on this path (motet#10), so its verdict
    is produced *after* the turn that caused it and has to reach the socket from a
    background task — and two coroutines writing to one WebSocket is a protocol violation
    waiting for a busy walk. One writer, FIFO, and the ordering falls out for free:
    ``put_nowait`` on an unbounded queue never yields, so a turn's own events are queued
    ahead of any verdict about them before the check has had a chance to run.
    """
    sender = asyncio.create_task(_deliver(websocket, session))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

            if (chunk := message.get("bytes")) is not None:
                for event in session.observe_audio(chunk):
                    session.outbox.put_nowait(event)
                continue

            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                session.outbox.put_nowait(_error(session, "bad_json", "control frame was not JSON"))
                continue
            if not isinstance(payload, dict):
                session.outbox.put_nowait(
                    _error(session, "bad_frame", "control frame must be an object")
                )
                continue

            if await _handle_control(session, payload):
                return
    finally:
        # Verdicts first, then the queue: an advisory check still running when the listener
        # hangs up is the case most worth counting, and letting it drop would bias the
        # ungrounded rate toward clean in exactly the wrong direction.
        await session.drain_grounding_checks()
        if not sender.done():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(FLUSH_TIMEOUT_SECONDS):
                    await session.outbox.join()
        if sender.done() and not sender.cancelled() and sender.exception() is not None:
            logger.error(
                "voice socket writer for session %s had already died: %r",
                session.session_id,
                sender.exception(),
            )
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)


async def _deliver(websocket: WebSocket, session: VoiceSession) -> None:
    """The one writer. Exits quietly when the socket has gone, rather than raising into it.

    **Anything that stops this task mutes the session**, because ``_pump`` goes on reading
    frames and queueing replies nobody sends. A disconnect is the expected way for that to
    happen and is not worth a stack trace; anything else is a bug and gets logged, because
    the alternative is a walk where every answer and every advisory verdict is silently
    dropped and nothing anywhere says so.
    """
    while True:
        event = await session.outbox.get()
        try:
            await _send(websocket, event)
        except (WebSocketDisconnect, RuntimeError):
            # The client left mid-flight. Nothing here is recoverable and nothing here is
            # the record: a verdict has already been counted and logged by the time it
            # reaches this queue, so what is lost is the client's copy and not the signal.
            session.outbox.task_done()
            _abandon(session.outbox)
            return
        except Exception:  # noqa: BLE001 — see the docstring: silence here is the failure
            logger.exception("voice socket writer failed; session %s is mute", session.session_id)
            session.outbox.task_done()
            _abandon(session.outbox)
            return
        session.outbox.task_done()


def _abandon(outbox: asyncio.Queue[SessionEvent]) -> None:
    """Mark whatever is still queued as done, because nothing will ever send it.

    Without this, ``join()`` in :func:`_pump` waits out its whole timeout on every
    disconnect that happened to have a backlog — five seconds of a Cloud Run concurrency
    slot spent waiting for a writer that has already given up.
    """
    while True:
        try:
            outbox.get_nowait()
        except asyncio.QueueEmpty:
            return
        outbox.task_done()


async def _handle_control(session: VoiceSession, payload: dict[str, Any]) -> bool:
    """Handle one control frame. Returns ``True`` when the session should end."""
    kind = str(payload.get("type", ""))

    if kind == "close":
        # Verdicts first: `closed` is the frame a well-behaved client tears down on, and
        # queueing it ahead of the advisory checks still in flight would drop from the wire
        # exactly the verdicts this path exists to deliver. `_pump`'s own drain stays as
        # the backstop for every other way a session ends.
        await session.drain_grounding_checks()
        session.outbox.put_nowait(
            SessionStateEvent(at_ms=session.clock.spoken_through_ms, state="closed")
        )
        return True

    if kind == "text":
        for event in await session.respond_to_text(str(payload.get("text", ""))):
            session.outbox.put_nowait(event)
        return False

    if kind == "barge_in":
        session.outbox.put_nowait(session.barge_in())
        return False

    if kind == "narration_delivered":
        session.narration_delivered(_as_int(payload.get("duration_ms")))
        return False

    if kind == "playback_position":
        # The client's own player, which is the only outside party that knows what came out
        # of the speaker. Not a provider — see clock.py.
        session.client_reported_position(_as_int(payload.get("spoken_through_ms")))
        return False

    if kind == "provider_position":
        # Recorded as drift and otherwise ignored. Invariant 4.
        session.provider_reported_position(_as_int(payload.get("spoken_through_ms")))
        return False

    session.outbox.put_nowait(_error(session, "unknown_frame", f"unknown control type {kind!r}"))
    return False


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _error(session: VoiceSession, code: str, message: str) -> ErrorEvent:
    return ErrorEvent(at_ms=session.clock.spoken_through_ms, code=code, message=message)


async def _send(websocket: WebSocket, event: SessionEvent) -> None:
    await websocket.send_text(event.model_dump_json())


async def _close(websocket: WebSocket, reason: str) -> None:
    logger.warning("rejecting voice socket: %s", reason)
    await websocket.send_text(
        json.dumps({"type": "error", "at_ms": 0, "code": "unauthorized", "message": reason})
    )
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


# The ASGI entry point is the *factory*, not a module-level app:
#
#     uvicorn motet_voice.app:create_app --factory --host 0.0.0.0 --port $PORT
#
# A module-level instance would build the arm and read the environment at import time,
# which makes the module unimportable in a context where those are not ready — including
# a test that wants to build the app with an injected arm.
