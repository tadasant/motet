"""A typed question must not depend on the live socket.

The realtime arm's live channel and its turn path are the *same* vendor socket, so when the
vendor refuses one it refuses the other — and before this existed the refusal walked out of
the WebSocket handler as an ASGI exception, closing the client's socket (1006) because a
vendor's had. The property pinned here is the boundary: a vendor exception anywhere inside a
turn becomes an ``error`` event, the session stays alive, and a typed question is answered
by the text arm (the composed arm, on the realtime arm) with the position block in its
prompt exactly as the live path would have had it.

The vendor is faked at the transport seam with a close-frame-shaped exception, so nothing
here reaches a network (invariant 7) and the reason code is derived from the same attributes
``websockets`` puts on its own.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from motet_voice.app import create_app
from motet_voice.config import VoiceSettings
from motet_voice.contract import StartSessionRequest
from motet_voice.live import failure_reason
from motet_voice.realtime import (
    ArmDormant,
    AssistantTurn,
    ComposedArm,
    LiveArm,
    TurnRequest,
    build_openai_arm,
)
from motet_voice.realtime.interfaces import ConversationModel
from motet_voice.session import VoiceSession
from motet_voice.tools import ToolRegistry

TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 20_000,
        "claims": [
            {"start_ms": 0, "end_ms": 10_000, "spoken_text": "Acme raised forty million."},
            {
                "start_ms": 10_000,
                "end_ms": 20_000,
                "spoken_text": "The company is valued at roughly 300 million dollars.",
            },
        ],
    },
]
CONFIG = StartSessionRequest.model_validate(
    {
        "persona": {"name": "Motet", "instructions": "Answer briefly.", "voice": "narrator"},
        "context": {
            "notes": "Acme raised forty million.",
            "transcript": TRANSCRIPT,
            "spoken_through_ms": 15_000,
        },
    }
)


@dataclass
class _CloseFrame:
    """The shape ``websockets`` gives ``ConnectionClosed.rcvd``."""

    code: int
    reason: str


class VendorRefused(Exception):
    """Stands in for ``websockets.exceptions.ConnectionClosedError``: same attributes."""

    def __init__(
        self, code: int = 1013, reason: str = "insufficient_quota.credit_balance_exhausted"
    ) -> None:
        super().__init__(f"received {code} (try again later) {reason}")
        self.rcvd = _CloseFrame(code, reason)


class RefusingTransport:
    """A vendor socket that will not open: every send raises the close error."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        raise VendorRefused()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        raise VendorRefused()
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


class DyingTransport:
    """Opens cleanly, then the socket dies: the next send after ``session.update`` raises,
    and the reader sees the same close."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.dead = False

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        if event["type"] == "session.update" and not self.dead:
            self._queue.put_nowait({"type": "session.created"})
            return
        self.dead = True
        self._queue.put_nowait(None)
        raise VendorRefused(1011, "server_error.internal")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._queue.get()
            if event is None:
                if self.dead:
                    raise VendorRefused(1011, "server_error.internal")
                return
            yield event

    async def aclose(self) -> None:
        self._queue.put_nowait(None)


@dataclass
class RecordingModel:
    """A text arm's LLM leg that keeps the request so the prompt can be asserted on."""

    requests: list[TurnRequest]

    @property
    def name(self) -> str:
        return "recording"

    def reply(self, request: TurnRequest, user_text: str) -> str:
        self.requests.append(request)
        return f"roughly 300 million dollars (you asked: {user_text})"


def _text_arm(model: ConversationModel) -> ComposedArm:
    from motet_inference.fakes import FakeSpeechSynthesizer  # noqa: PLC0415

    return ComposedArm(model=model, synthesizer=FakeSpeechSynthesizer(), conversational=True)


def _session(settings: VoiceSettings, transport: Any, text_arm: ComposedArm | None) -> VoiceSession:
    arm = build_openai_arm(settings, transport=transport)
    assert isinstance(arm, LiveArm)
    return VoiceSession.create(
        session_id="vs_fallback", config=CONFIG, arm=arm, tools=ToolRegistry({}), text_arm=text_arm
    )


# ------------------------------------------------------------------ the session on its own


def test_a_live_channel_that_will_not_open_hands_typed_turns_to_the_text_arm(
    settings: VoiceSettings,
) -> None:
    """Vendor refuses at open → ready says so with a reason → the typed turn is answered by
    the text arm, with the position block, and no exception escapes."""
    requests: list[TurnRequest] = []
    transport = RefusingTransport()
    session = _session(settings, transport, _text_arm(RecordingModel(requests)))

    async def run() -> list[Any]:
        await session.start_live()
        ready = session.ready()
        events = await session.respond_to_text("how many dollars?")
        return [ready, events]

    ready, events = asyncio.run(run())

    assert session.live is None
    assert ready.state == "ready"
    assert ready.reason == "insufficient_quota"
    assert ready.detail is not None
    assert ready.detail.startswith(
        "live conversation unavailable (insufficient_quota); "
        "answering typed questions with the composed arm"
    )
    # The vendor was asked to open exactly once — the typed turn did not go back to it.
    assert [s["type"] for s in transport.sent] == ["session.update"]

    kinds = [(e.type, getattr(e, "speaker", None)) for e in events]
    assert kinds == [("transcript", "user"), ("transcript", "assistant"), ("audio_chunk", None)]
    assert "roughly 300 million dollars" in events[1].text
    # The position context reached the fallback's prompt exactly as the live path frames it.
    assert len(requests) == 1
    assert "interrupted the briefing at 0:15" in requests[0].position_notes
    assert "roughly 300 million dollars" in requests[0].position_notes
    assert requests[0].user_text == "how many dollars?"
    assert session.summary()["text_arm"] == "composed"
    assert session.summary()["live_failure_reason"] == "insufficient_quota"


def test_a_live_socket_dying_mid_turn_is_a_turn_failed_event_and_the_session_lives(
    settings: VoiceSettings,
) -> None:
    """Open → the socket dies on the typed turn → ``turn_failed`` (no exception) → the next
    typed turn tries one reopen, which the dead vendor refuses, and goes to the text arm."""
    requests: list[TurnRequest] = []
    transport = DyingTransport()
    session = _session(settings, transport, _text_arm(RecordingModel(requests)))

    async def run() -> tuple[Any, list[Any], bool, list[Any]]:
        await session.start_live()
        ready = session.ready()
        first = await session.respond_to_text("how many dollars?")
        await asyncio.sleep(0)  # let the reader notice the close
        failed_between = session.live is not None and bool(session.live.failed)
        second = await session.respond_to_text("how many dollars?")
        await session.aclose()
        return ready, first, failed_between, second

    ready, first, failed_between, second = asyncio.run(run())

    assert ready.detail is not None and ready.detail.startswith("live conversation open")
    assert ready.reason is None
    assert [e.type for e in first] == ["error"]
    assert first[0].code == "turn_failed"
    assert "server_error" in first[0].message
    assert "composed arm" in first[0].message
    assert failed_between
    # A channel that *opened* and then died is reopened once, at the next question — and
    # the vendor still being down costs that one attempt, not the answer.
    assert session.summary()["live_reopens"] == 1
    assert session.live is None
    # The second turn is answered — by the text arm, with the position block.
    assert [e.type for e in second] == ["transcript", "transcript", "audio_chunk"]
    assert len(requests) == 1
    assert "interrupted the briefing at 0:15" in requests[0].position_notes


def test_a_live_arm_with_no_text_arm_says_it_cannot_answer_rather_than_retrying(
    settings: VoiceSettings,
) -> None:
    transport = RefusingTransport()
    session = _session(settings, transport, None)

    async def run() -> list[Any]:
        await session.start_live()
        return await session.respond_to_text("how many dollars?")

    events = asyncio.run(run())
    assert [e.type for e in events] == ["error"]
    assert events[0].code == "arm_dormant"
    assert "no arm in this process" in events[0].message
    assert [s["type"] for s in transport.sent] == ["session.update"], "the vendor was retried"
    ready = session.ready()
    assert ready.reason == "insufficient_quota"
    assert "no arm can answer" in (ready.detail or "")


def test_failure_reasons_are_short_codes_not_prose() -> None:
    assert failure_reason(VendorRefused()) == "insufficient_quota"
    assert failure_reason(VendorRefused(1011, "server_error.internal")) == "server_error"
    assert failure_reason(VendorRefused(1013, "")) == "close_1013"
    assert failure_reason(ArmDormant("OPENAI_API_KEY is not set")) == "arm_dormant"
    assert failure_reason(TimeoutError()) == "timeout_error"
    assert failure_reason(OSError("boom")) == "os_error"


# ------------------------------------------------------------- through the real WebSocket


def test_over_the_socket_a_vendor_refusal_never_closes_the_client(settings: VoiceSettings) -> None:
    """The ASGI shape of the bug: the client's WebSocket stays open through a refused live
    open *and* a typed turn, and gets a reply from the text arm."""
    requests: list[TurnRequest] = []
    arm = build_openai_arm(settings, transport=RefusingTransport())
    app = create_app(settings, arm=arm, text_arm=_text_arm(RecordingModel(requests)))
    body = CONFIG.model_dump(mode="json")

    with TestClient(app) as client:
        health = client.get("/internal/health").json()
        assert health["arm"] == "openai_realtime"
        assert health["text_arm"] == "composed"
        started = client.post("/v1/voice/sessions", json=body)
        assert started.status_code == 201, started.text
        session_id = started.json()["session_id"]
        with client.websocket_connect(f"/v1/voice/sessions/{session_id}/stream") as socket:
            token = started.json()["session_token"]
            socket.send_text(json.dumps({"type": "authenticate", "token": token, "config": body}))
            ready = json.loads(socket.receive_text())
            assert ready["state"] == "ready"
            assert ready["reason"] == "insufficient_quota"
            assert ready["detail"].startswith("live conversation unavailable (insufficient_quota)")

            socket.send_text(json.dumps({"type": "barge_in"}))
            interrupted = json.loads(socket.receive_text())
            assert interrupted["type"] == "interrupted_at"

            socket.send_text(json.dumps({"type": "text", "text": "how many dollars?"}))
            events = [json.loads(socket.receive_text()) for _ in range(3)]
            assert [e["type"] for e in events] == ["transcript", "transcript", "audio_chunk"]
            assert "roughly 300 million dollars" in events[1]["text"]

            socket.send_text(json.dumps({"type": "close"}))
            assert json.loads(socket.receive_text())["state"] == "closed"


def test_the_composed_arm_is_its_own_text_arm(settings: VoiceSettings) -> None:
    """No live channel on the composed arm, and nothing about it changed: it answers itself."""
    from motet_voice.realtime import build_composed_arm  # noqa: PLC0415

    arm = build_composed_arm(settings)
    session = VoiceSession.create(
        session_id="vs_composed", config=CONFIG, arm=arm, tools=ToolRegistry({})
    )
    assert session.text_arm is arm
    ready = session.ready()
    assert ready.reason is None
    assert not (ready.detail or "").startswith("live conversation")
    events = asyncio.run(session.respond_to_text("how many dollars?"))
    assert [e.type for e in events][:2] == ["transcript", "transcript"]


def test_the_app_builds_the_composed_arm_behind_a_live_arm(settings: VoiceSettings) -> None:
    arm = build_openai_arm(settings, transport=RefusingTransport())
    app = create_app(settings, arm=arm)
    with TestClient(app) as client:
        health = client.get("/internal/health").json()
    assert health["text_arm"] == "composed"


def test_an_assistant_turn_is_untouched_by_the_reason_plumbing() -> None:
    """Guard: the value type the text arm returns did not grow a field by accident."""
    assert AssistantTurn(text="x").audio is None


# ------------------------------------------------------------- reopening (PR review)


class _Channel:
    """A live conversation that records what it was sent and never says anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def append_audio(self, pcm: bytes) -> None:
        self.calls.append(("append", len(pcm)))

    async def add_context(self, text: str) -> None:
        self.calls.append(("context", None))

    async def add_user_text(self, text: str) -> None:
        self.calls.append(("user", text))

    async def tool_output(self, call_id: str, output: Mapping[str, Any]) -> None:
        self.calls.append(("tool", call_id))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.calls.append(("truncate", item_id))

    async def cancel_response(self) -> None:
        self.calls.append(("cancel", None))

    async def events(self) -> AsyncIterator[Any]:
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def aclose(self) -> None:
        self.calls.append(("close", None))


def _reopenable_session(channels: list[_Channel]) -> VoiceSession:
    from motet_voice.realtime import OpenAiRealtimeArm  # noqa: PLC0415

    def factory(_request: TurnRequest) -> _Channel:
        channels.append(_Channel())
        return channels[-1]

    arm = OpenAiRealtimeArm(model="test", conversation_factory=factory)
    return VoiceSession.create(
        session_id="vs_reopen", config=CONFIG, arm=arm, tools=ToolRegistry({})
    )


def test_a_channel_reopened_at_a_barge_in_carries_the_pre_roll() -> None:
    channels: list[_Channel] = []
    session = _reopenable_session(channels)

    async def run() -> None:
        await session.start_live()
        assert session.live is not None
        session.live.remember(bytes(3_200 * 3))
        session.live.failed = "socket dropped"
        session.live.failure_reason = "connection_closed"
        await session.client_barge_in()

    asyncio.run(run())
    assert len(channels) == 2
    assert session.summary()["live_reopens"] == 1
    assert ("append", 9_600) in channels[1].calls, "the first word was lost to the reopen"


def test_a_channel_that_died_for_want_of_credit_is_not_reopened() -> None:
    channels: list[_Channel] = []
    session = _reopenable_session(channels)

    async def run() -> None:
        await session.start_live()
        assert session.live is not None
        session.live.failed = "insufficient_quota.credit_balance_exhausted"
        session.live.failure_reason = "insufficient_quota"
        await session.client_barge_in()

    asyncio.run(run())
    assert len(channels) == 1
    assert session.summary()["live_reopens"] == 0
    assert session.summary()["live_failure_reason"] == "insufficient_quota"
