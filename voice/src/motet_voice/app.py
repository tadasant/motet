"""The voice service as an HTTP + WebSocket app, built to run on Cloud Run.

```
POST /v1/voice/sessions                      StartSession(...) -> session_token
WS   /v1/voice/sessions/{session_id}/stream  audio in; transcripts, tool calls,
                                             audio chunks and interrupted_at out
GET  /healthz                                what is wired, and what is dormant
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
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError

from . import obs, tokens
from .config import VoiceSettings, load_settings
from .contract import (
    PLATFORM_TOOLS,
    ErrorEvent,
    SessionEvent,
    SessionStateEvent,
    StartSessionRequest,
    StartSessionResponse,
)
from .realtime import RealtimeArm, build_arm
from .session import VoiceSession
from .tools import HttpToolTransport, ToolRegistry, ToolTransport, build_platform_tools

logger = logging.getLogger("motet.voice.app")

WEBSOCKET_PATH = "/v1/voice/sessions/{session_id}/stream"

#: How long an accepted socket may go without authenticating. Short: a client that
#: has just been handed a token sends it immediately.
AUTHENTICATE_TIMEOUT_SECONDS = 10.0


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
    ) -> None:
        self.settings = settings or load_settings()
        self.arm = arm or build_arm(self.settings)
        self._explicit_transport = transport is not None
        self.transport = transport or self._build_transport()

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
) -> FastAPI:
    """Build the ASGI app. Injectable so tests never touch a network or a vendor."""
    state = VoiceApp(settings, arm=arm, transport=transport)

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
        yield
        await state.aclose()

    app = FastAPI(
        lifespan=lifespan,
        title="Motet Voice",
        version="0.1.0",
        description=(
            "The voice session contract. Clients speak this and never a vendor protocol; "
            "the service holds no database credential and reaches Motet only through tools."
        ),
    )
    app.state.voice = state

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    def healthz() -> HealthResponse:
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
            inference_mode=state.settings.inference_mode,
            arm=capabilities.name,
            arm_conversational=capabilities.conversational,
            arm_dormant_reason=capabilities.dormant_reason,
            session_secret_configured=state.settings.session_secret_provided,
            tools=registry.describe(),
        )

    @app.post(
        "/v1/voice/sessions",
        response_model=StartSessionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["session"],
    )
    def start_session(request: StartSessionRequest) -> StartSessionResponse:
        """``StartSession(persona, tools, mcp_servers, context, turn_policy) -> token``."""
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
        session_id=session_id, config=config, arm=state.arm, tools=state.registry(config)
    )
    await _send(websocket, session.ready())
    return session


async def _pump(websocket: WebSocket, session: VoiceSession) -> None:
    """The session loop: audio frames in, events out.

    Binary frames are listener audio. Text frames are control messages. Anything
    unrecognized gets an ``error`` event rather than a closed socket — a client that sends
    one bad message should not lose a walk.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        if (chunk := message.get("bytes")) is not None:
            for event in session.observe_audio(chunk):
                await _send(websocket, event)
            continue

        text = message.get("text")
        if text is None:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            await _send(websocket, _error(session, "bad_json", "control frame was not JSON"))
            continue
        if not isinstance(payload, dict):
            await _send(websocket, _error(session, "bad_frame", "control frame must be an object"))
            continue

        if await _handle_control(websocket, session, payload):
            return


async def _handle_control(
    websocket: WebSocket, session: VoiceSession, payload: dict[str, Any]
) -> bool:
    """Handle one control frame. Returns ``True`` when the session should end."""
    kind = str(payload.get("type", ""))

    if kind == "close":
        await _send(
            websocket,
            SessionStateEvent(at_ms=session.clock.spoken_through_ms, state="closed"),
        )
        return True

    if kind == "text":
        for event in await session.respond_to_text(str(payload.get("text", ""))):
            await _send(websocket, event)
        return False

    if kind == "barge_in":
        await _send(websocket, session.barge_in())
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

    await _send(websocket, _error(session, "unknown_frame", f"unknown control type {kind!r}"))
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
