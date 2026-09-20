"""The live, speech-to-speech path: listener audio in, streamed reply out, no vendor frame
ever reaching the client.

Driven through the real WebSocket against a *reactive* scripted vendor — one that answers
what it is sent rather than replaying a list — because the property under test is an
ordering: the position block goes in before the audio, the audio is forwarded only after
the barge-in, and the reply streams back chunk by chunk before the transcript lands.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from motet_voice.app import create_app
from motet_voice.clock import PlaybackClock
from motet_voice.config import VoiceSettings
from motet_voice.live import PREROLL_MS, LiveBridge
from motet_voice.realtime import (
    AssistantAudio,
    AssistantTranscript,
    LiveArm,
    OpenAiLiveConversation,
    SpeechStarted,
    SpeechStopped,
    TurnDone,
    build_composed_arm,
    build_openai_arm,
)
from motet_voice.tools import ToolRegistry

PERSONA = {"name": "Motet", "instructions": "Answer briefly.", "voice": "narrator"}
TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 20_000,
        "claims": [
            {"start_ms": 0, "end_ms": 10_000, "spoken_text": "Acme raised forty million."},
            {"start_ms": 10_000, "end_ms": 20_000, "spoken_text": "Example Ventures led."},
        ],
    },
    {
        "title": "Helion's timeline",
        "start_ms": 20_000,
        "end_ms": 40_000,
        "claims": [{"start_ms": 20_000, "end_ms": 40_000, "spoken_text": "Helion targets 2028."}],
    },
]
CONTRACT_EVENT_TYPES = {
    "session_state",
    "transcript",
    "audio_chunk",
    "tool_call",
    "tool_result",
    "interrupted_at",
    "error",
}

#: 100 ms of 24 kHz mono int16.
REPLY_CHUNK = bytes(4_800)


def _reply_script(*, cancelled: bool = False) -> list[dict[str, Any]]:
    """The vendor's side of one turn, in the GA event names."""
    return [
        {"type": "input_audio_buffer.speech_started", "audio_start_ms": 120},
        {"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 1_900},
        {"type": "input_audio_buffer.committed"},
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "what did the company raise",
        },
        {"type": "response.created"},
        *(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(REPLY_CHUNK).decode()}
            for _ in range(3)
        ),
        {"type": "response.output_audio.done"},
        {
            "type": "response.output_audio_transcript.done",
            "transcript": "Acme raised forty million dollars.",
        },
        {
            "type": "response.done",
            "response": {
                "status": "cancelled" if cancelled else "completed",
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 60,
                    "input_token_details": {"audio_tokens": 200, "cached_tokens": 512},
                    "output_token_details": {"audio_tokens": 50},
                },
            },
        },
    ]


class ReactiveTransport:
    """A scripted vendor that answers what it is sent.

    ``session.update`` gets ``session.created``; enough appended audio gets a whole reply;
    ``response.create`` (the typed path) gets the same reply. Everything sent is recorded.
    """

    def __init__(self, *, reply_after_bytes: int = 24_000 * 2) -> None:
        self.sent: list[dict[str, Any]] = []
        self.appended = 0
        self.closed = False
        self.reply_after_bytes = reply_after_bytes
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._replied = False

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        kind = event["type"]
        if kind == "session.update":
            self._queue.put_nowait({"type": "session.created"})
            self._queue.put_nowait({"type": "session.updated"})
        elif kind == "input_audio_buffer.append":
            self.appended += len(base64.b64decode(event["audio"]))
            if self.appended >= self.reply_after_bytes and not self._replied:
                self._replied = True
                for scripted in _reply_script():
                    self._queue.put_nowait(scripted)
        elif kind == "response.create" and not self._replied:
            self._replied = True
            for scripted in _reply_script()[4:]:
                self._queue.put_nowait(scripted)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def aclose(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


@contextmanager
def _live_socket(settings: VoiceSettings, transport: ReactiveTransport) -> Iterator[Any]:
    arm = build_openai_arm(settings, transport=transport)
    assert isinstance(arm, LiveArm)
    app = create_app(settings, arm=arm)
    body = {
        "persona": PERSONA,
        "context": {
            "notes": "Acme raised forty million. Helion targets 2028.",
            "transcript": TRANSCRIPT,
            "spoken_through_ms": 15_000,
        },
    }
    with TestClient(app) as client:
        started = client.post("/v1/voice/sessions", json=body)
        assert started.status_code == 201, started.text
        session_id = started.json()["session_id"]
        with client.websocket_connect(f"/v1/voice/sessions/{session_id}/stream") as socket:
            socket.send_text(
                json.dumps(
                    {
                        "type": "authenticate",
                        "token": started.json()["session_token"],
                        "config": body,
                    }
                )
            )
            ready = json.loads(socket.receive_text())
            assert ready["state"] == "ready"
            assert ready["detail"].startswith("live conversation open")
            try:
                yield socket
            finally:
                socket.close()


def _until_ready(socket: Any) -> list[dict[str, Any]]:
    events = []
    while True:
        event = json.loads(socket.receive_text())
        events.append(event)
        if event["type"] == "session_state" and event["state"] == "ready":
            return events


def test_spoken_question_streams_back_a_spoken_reply(settings: VoiceSettings) -> None:
    """barge_in → position in → audio forwarded → transcripts and pcm16 chunks out → ready."""
    transport = ReactiveTransport()
    with _live_socket(settings, transport) as socket:
        # No `narration_delivered`: it would start the wall clock, and the offset asserted
        # below would drift by however many milliseconds the barge-in frame took to arrive.
        # The session's seek to 15 s is the delivered ceiling, so the clock holds there.
        socket.send_text(json.dumps({"type": "barge_in"}))
        interrupted = json.loads(socket.receive_text())
        listening = json.loads(socket.receive_text())

        # Two seconds of listener audio in 200 ms packets, as a client sends it.
        packet = bytes(16_000 * 2 // 5)
        for _ in range(10):
            socket.send_bytes(packet)
        events = _until_ready(socket)
        socket.send_text(json.dumps({"type": "close"}))
        assert json.loads(socket.receive_text())["state"] == "closed"

    assert interrupted["type"] == "interrupted_at"
    assert interrupted["offset_ms"] == 15_000
    assert interrupted["context"] == {
        "clock": "0:15",
        "segment_title": "Acme raises a Series B",
        "claim_text": "Example Ventures led.",
    }
    assert listening == {
        "type": "session_state",
        "at_ms": 15_000,
        "state": "listening",
        "detail": "live — speak your question",
        "reason": None,
        # Only the first `ready` answers this; every other frame says nothing, as it does
        # for `reason`. Asserted rather than omitted because this is the whole wire shape.
        "can_answer": None,
        "live": True,
    }

    kinds = [(e["type"], e.get("state") or e.get("speaker")) for e in events]
    assert kinds[0] == ("transcript", "user")
    assert events[0]["text"] == "what did the company raise"
    assert kinds[1] == ("session_state", "speaking")
    chunks = [e for e in events if e["type"] == "audio_chunk"]
    assert len(chunks) == 3, "each vendor delta is relayed as it arrives, not batched"
    assert all(c["format"] == "pcm16" and c["sample_rate"] == 24_000 for c in chunks)
    assert all(c["duration_ms"] == 100 for c in chunks)
    assert base64.b64decode(chunks[0]["pcm_base64"]) == REPLY_CHUNK
    assistant = [e for e in events if e["type"] == "transcript" and e["speaker"] == "assistant"]
    assert [e["text"] for e in assistant] == ["Acme raised forty million dollars."]
    assert events[-1] == {
        "type": "session_state",
        "at_ms": 15_000,
        "state": "ready",
        "detail": "reply complete",
        "reason": None,
        # A later `ready` is "the reply finished streaming", not a fresh session, so it
        # re-answers nothing: the client keeps what the first one told it.
        "can_answer": None,
        "live": None,
    }
    assert {e["type"] for e in events} <= CONTRACT_EVENT_TYPES, (
        "the client saw a vendor event type (invariant 1)"
    )

    # What went to the vendor, and in what order.
    kinds_sent = [s["type"] for s in transport.sent]
    assert kinds_sent[0] == "session.update"
    assert transport.sent[0]["session"]["type"] == "realtime"
    first_context = kinds_sent.index("conversation.item.create")
    first_append = kinds_sent.index("input_audio_buffer.append")
    assert first_context < first_append, "the position has to be in place before the audio"
    context_item = transport.sent[first_context]["item"]
    assert context_item["role"] == "system"
    text = context_item["content"][0]["text"]
    assert "interrupted the briefing at 0:15" in text
    assert "'Acme raises a Series B'" in text
    assert "Example Ventures led." in text
    assert "Stories not yet reached: Helion's timeline." in text
    # 16 kHz in, 24 kHz out: half again as many bytes reach the vendor — for every packet
    # forwarded. Packets that arrive after the reply has ended are not forwarded at all
    # (the floor is no longer the listener's), so the total is a multiple, not the whole.
    assert transport.appended >= transport.reply_after_bytes
    assert transport.appended % (len(packet) * 3 // 2) == 0
    assert transport.appended < 10 * len(packet) * 3 // 2, (
        "audio kept flowing to the vendor after the reply was complete"
    )
    assert "response.create" not in kinds_sent, "the vendor's VAD ends the turn, not us"
    assert transport.closed


def test_a_typed_question_goes_down_the_live_channel_and_streams_back(
    settings: VoiceSettings,
) -> None:
    """The typed box stays as the fallback, and its reply is spoken the same way."""
    transport = ReactiveTransport()
    with _live_socket(settings, transport) as socket:
        socket.send_text(json.dumps({"type": "text", "text": "who led the round"}))
        events = _until_ready(socket)

    assert events[0] == {
        "type": "transcript",
        "at_ms": 15_000,
        "speaker": "user",
        "text": "who led the round",
        "final": True,
    }
    assert [e["type"] for e in events].count("audio_chunk") == 3
    sent = [s["type"] for s in transport.sent]
    assert sent[-2:] == ["conversation.item.create", "response.create"]
    user_item = transport.sent[-2]["item"]
    assert user_item["role"] == "user"
    assert user_item["content"][0]["text"] == "who led the round"
    # And the position went in ahead of the question, as a system item.
    system_items = [
        s["item"]
        for s in transport.sent
        if s["type"] == "conversation.item.create"
        if s["item"].get("role") == "system"
    ]
    assert len(system_items) == 1
    assert "interrupted the briefing at 0:15" in system_items[0]["content"][0]["text"]


def test_the_composed_arm_still_runs_the_turn_path_with_a_position_block(
    settings: VoiceSettings,
) -> None:
    """No live channel on the composed arm, and the position reaches its prompt anyway."""
    arm = build_composed_arm(settings)
    assert not isinstance(arm, LiveArm)
    from motet_voice.realtime.composed import _system_prompt  # noqa: PLC0415
    from motet_voice.realtime.interfaces import TurnRequest  # noqa: PLC0415

    prompt = _system_prompt(
        TurnRequest(persona_instructions="p", voice="narrator", position_notes="POSITION BLOCK")
    )
    assert "POSITION BLOCK" in prompt
    assert "POSITION BLOCK" not in _system_prompt(
        TurnRequest(persona_instructions="p", voice="narrator")
    )


# ---------------------------------------------------------------- the bridge on its own


class _Recording:
    """A LiveConversation that records and yields nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def append_audio(self, pcm: bytes) -> None:
        self.calls.append(("append", pcm))

    async def add_context(self, text: str) -> None:
        self.calls.append(("context", text))

    async def add_user_text(self, text: str) -> None:
        self.calls.append(("user", text))

    async def tool_output(self, call_id: str, output: Mapping[str, Any]) -> None:
        self.calls.append(("tool", call_id))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.calls.append(("truncate", (item_id, audio_end_ms)))

    async def cancel_response(self) -> None:
        self.calls.append(("cancel", None))

    async def events(self) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    async def aclose(self) -> None:
        self.calls.append(("close", None))


def _bridge(conversation: _Recording) -> LiveBridge:
    return LiveBridge.open(
        session_id="s",
        arm_name="test",
        conversation=conversation,
        tools=ToolRegistry({}),
        clock=PlaybackClock(),
        outbox=asyncio.Queue(),
        history=[],
    )


def test_the_preroll_keeps_the_first_word_and_is_bounded() -> None:
    """Audio heard *before* the barge-in reaches the vendor first, capped at PREROLL_MS."""

    async def scenario() -> None:
        conversation = _Recording()
        bridge = _bridge(conversation)
        # 2 s of audio in 100 ms packets, none of it forwarded yet.
        for index in range(20):
            bridge.remember(bytes([index]) * 3_200)
        await bridge.forward(b"\xff" * 3_200)
        assert conversation.calls == [], "nothing is forwarded before the floor is taken"

        await bridge.engage("POSITION")
        kinds = [kind for kind, _ in conversation.calls]
        assert kinds == ["context", "append"]
        flushed = conversation.calls[1][1]
        assert len(flushed) <= PREROLL_MS * 32 + 3_200
        assert len(flushed) >= PREROLL_MS * 32
        assert flushed.endswith(bytes([19]) * 3_200), "the newest audio is what is kept"
        assert bridge.active

        await bridge.forward(b"\x01\x02" * 100)
        assert conversation.calls[-1] == ("append", b"\x01\x02" * 100)

    asyncio.run(scenario())


def test_a_reply_the_listener_talked_over_does_not_resume_narration() -> None:
    """`response.done{status: cancelled}` is not the end of the listener's turn."""

    async def scenario() -> None:
        conversation = _Recording()
        bridge = _bridge(conversation)
        states = [event.model_dump()["state"] for event in await bridge.engage("")]
        await bridge._handle(AssistantAudio(pcm=REPLY_CHUNK, sample_rate=24_000, item_id="r1"))
        await bridge._handle(SpeechStarted(audio_start_ms=3_000))
        await bridge._handle(TurnDone(cancelled=True))
        # The transcript of the whole generated reply lands after the cut.
        await bridge._handle(
            AssistantTranscript(text="Acme raised forty million dollars.", item_id="r1")
        )

        while not bridge.outbox.empty():
            event = bridge.outbox.get_nowait()
            if event.type == "session_state":
                states.append(event.model_dump()["state"])
        assert states == ["listening", "speaking", "listening"], (
            "the interruption flushes the client's queue; nothing says `ready`"
        )
        assert bridge.active, "the floor is still the listener's"
        assert bridge.speech_starts == 1
        assert bridge.replies_cut == 1
        # The vendor is told how much was heard, bounded by what was generated (100 ms).
        truncations = [arg for kind, arg in conversation.calls if kind == "truncate"]
        assert len(truncations) == 1
        assert truncations[0][0] == "r1"
        assert 0 <= truncations[0][1] <= 100
        # And our own history says it was cut, rather than recording it as spoken.
        assert bridge.history[-1]["role"] == "assistant"
        assert bridge.history[-1]["text"].endswith("[cut off by the listener]")

        await bridge._handle(SpeechStopped(audio_end_ms=4_500))
        await bridge._handle(TurnDone(usage={"input_tokens": 10, "output_tokens": 5}))
        assert not bridge.active
        assert bridge.summary()["live_usage"]["input_tokens"] == 10

    asyncio.run(scenario())


def test_the_arm_upsamples_to_the_provider_rate_and_reads_both_name_families(
    settings: VoiceSettings,
) -> None:
    transport = ReactiveTransport(reply_after_bytes=10**9)
    arm = build_openai_arm(settings, transport=transport)
    from motet_voice.realtime.interfaces import TurnRequest  # noqa: PLC0415

    live = arm.open_live(TurnRequest(persona_instructions="p", voice="cedar"))
    assert isinstance(live, OpenAiLiveConversation)

    async def scenario() -> None:
        await live.start()
        await live.append_audio(bytes(3_200))  # 100 ms at 16 kHz
        appended = base64.b64decode(transport.sent[-1]["audio"])
        assert len(appended) == 4_800, "100 ms at 24 kHz"

        old = live._translate(
            {"type": "response.audio.delta", "delta": base64.b64encode(b"ab").decode()}
        )
        new = live._translate(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"ab").decode()}
        )
        assert isinstance(old, AssistantAudio) and isinstance(new, AssistantAudio)
        assert old.pcm == new.pcm == b"ab"
        assert live._translate({"type": "rate_limits.updated"}) is None
        error = live._translate(
            {
                "type": "error",
                "error": {"code": "credit_balance_exhausted", "message": "no credits"},
            }
        )
        assert error is not None and error.code == "credit_balance_exhausted"

    asyncio.run(scenario())
    assert transport.sent[0]["session"]["audio"]["output"]["voice"] == "cedar", (
        "a label that is already a vendor voice passes through"
    )


@pytest.mark.parametrize(
    "label,expected", [("narrator", "marin"), ("NARRATOR", "marin"), ("x", "marin")]
)
def test_voice_labels_never_reach_the_vendor_unmapped(label: str, expected: str) -> None:
    from motet_voice.realtime.openai_realtime import vendor_voice  # noqa: PLC0415

    assert vendor_voice(label) == expected


def test_every_realtime_response_is_a_metric_split_by_token_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 11: the realtime bill is a metric, not only a log line — and every kind is
    added to even at zero, so a panel never reads a missing series as a zero."""
    from motet_voice import live as live_module  # noqa: PLC0415

    tokens: list[tuple[int, dict[str, str]]] = []
    replies: list[dict[str, str]] = []

    class _Counter:
        def __init__(self, sink: list[Any], *, with_amount: bool) -> None:
            self.sink = sink
            self.with_amount = with_amount

        def add(self, amount: int, attributes: dict[str, str]) -> None:
            self.sink.append((amount, attributes) if self.with_amount else attributes)

    monkeypatch.setattr(live_module, "_realtime_tokens", _Counter(tokens, with_amount=True))
    monkeypatch.setattr(live_module, "_realtime_replies", _Counter(replies, with_amount=False))

    async def scenario() -> None:
        bridge = _bridge(_Recording())
        await bridge.engage("")
        await bridge._handle(
            TurnDone(
                usage={
                    "input_tokens": 900,
                    "output_tokens": 60,
                    "input_token_details": {"audio_tokens": 200, "cached_tokens": 512},
                    "output_token_details": {"audio_tokens": 50},
                }
            )
        )
        await bridge._handle(TurnDone(cancelled=True))

    asyncio.run(scenario())
    first = {attrs["kind"]: amount for amount, attrs in tokens[:5]}
    assert first == {
        "input_text": 700,
        "input_audio": 200,
        "input_cached": 512,
        "output_text": 10,
        "output_audio": 50,
    }
    assert len(tokens) == 10, "a response with no usage still adds a zero per kind"
    assert all(attrs["arm"] == "test" for _, attrs in tokens)
    assert [r["outcome"] for r in replies] == ["completed", "cancelled"]


def test_never_mind_during_a_reply_cancels_it_and_records_it_as_cut() -> None:
    """Resuming narration mid-reply closes the gate, cancels the reply, and the reply's
    transcript — arriving afterwards — enters history as cut rather than as spoken."""

    async def scenario() -> None:
        conversation = _Recording()
        bridge = _bridge(conversation)
        await bridge.engage("")
        await bridge._handle(AssistantAudio(pcm=REPLY_CHUNK, sample_rate=24_000, item_id="r2"))
        await bridge.disengage()
        assert not bridge.active
        kinds = [kind for kind, _ in conversation.calls]
        assert kinds[-2:] == ["truncate", "cancel"]
        # Late chunks from the cancelled reply are nobody's to play.
        before = bridge.outbox.qsize()
        await bridge._handle(AssistantAudio(pcm=REPLY_CHUNK, sample_rate=24_000, item_id="r2"))
        assert bridge.outbox.qsize() == before
        await bridge._handle(AssistantTranscript(text="Acme raised forty million.", item_id="r2"))
        await bridge._handle(TurnDone(cancelled=True))
        assert bridge.history[-1]["text"].endswith("[cut off by the listener]")
        assert bridge.replies == 0

    asyncio.run(scenario())


# ------------------------------------------------------------- found by the PR review


class _ToolTurnTransport:
    """A vendor that asks for a tool, reports that response done, and only answers — in a
    second response — once the tool's output arrives. The order the real vendor uses."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def say(self, *events: dict[str, Any]) -> None:
        for event in events:
            self._queue.put_nowait(event)

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(dict(event))
        item = event.get("item") or {}
        if (
            event["type"] == "conversation.item.create"
            and item.get("type") == "function_call_output"
        ):
            self.say(
                {"type": "response.created"},
                {
                    "type": "response.output_audio.delta",
                    "item_id": "answer",
                    "delta": base64.b64encode(REPLY_CHUNK).decode(),
                },
                {
                    "type": "response.output_audio_transcript.done",
                    "item_id": "answer",
                    "transcript": "Marked it read.",
                },
                {"type": "response.done", "response": {"status": "completed"}},
            )

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def aclose(self) -> None:
        self._queue.put_nowait(None)


def test_a_tool_calls_answer_is_still_played_after_the_tool_runs() -> None:
    """The calling response's `response.done` arrives after the tool ran; it must not end
    the turn, or the answer — billed — is dropped and narration resumes over it."""

    async def scenario() -> list[Any]:
        transport = _ToolTurnTransport()
        conversation = OpenAiLiveConversation(
            transport=transport, session_update={"type": "session.update"}
        )
        bridge = _bridge(conversation)  # type: ignore[arg-type]
        await bridge.start()
        await bridge.engage("")
        transport.say(
            {"type": "response.created"},
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c1",
                "name": "mark_read",
                "arguments": '{"news_item_id": "ni_1"}',
            },
            {"type": "response.done", "response": {"status": "completed"}},
        )
        events: list[Any] = []
        while True:
            event = await asyncio.wait_for(bridge.outbox.get(), timeout=2)
            events.append(event.model_dump())
            if event.type == "session_state" and event.model_dump()["state"] == "ready":
                break
        assert bridge.replies == 1 and not bridge.active and not bridge.reply_owed
        await bridge.aclose()
        return events

    events = asyncio.run(scenario())
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["tool_call", "tool_result"]
    assert "audio_chunk" in kinds, "the tool's answer was dropped"
    assert kinds.index("audio_chunk") < len(kinds) - 1
    assert events[-1]["detail"] == "reply complete"


def test_resume_with_nothing_said_does_not_cancel_a_response_that_does_not_exist() -> None:
    """`response.cancel` with nothing in flight is answered by the vendor with an error; the
    common "never mind" right after a barge-in must only clear the buffer."""
    transport = _ToolTurnTransport()
    live = OpenAiLiveConversation(transport=transport, session_update={"type": "session.update"})

    async def scenario() -> None:
        await live.cancel_response()
        assert [s["type"] for s in transport.sent] == ["input_audio_buffer.clear"]
        live._translate({"type": "response.created"})
        await live.cancel_response()
        assert [s["type"] for s in transport.sent][-2:] == [
            "response.cancel",
            "input_audio_buffer.clear",
        ]
        live._translate({"type": "response.done", "response": {"status": "cancelled"}})
        await live.cancel_response()
        assert [s["type"] for s in transport.sent][-1] == "input_audio_buffer.clear"
        assert [s["type"] for s in transport.sent].count("response.cancel") == 1

    asyncio.run(scenario())


def test_a_typed_question_owes_a_reply_without_opening_the_mic() -> None:
    async def scenario() -> None:
        conversation = _Recording()
        bridge = _bridge(conversation)
        await bridge.ask("who led?", "")
        assert bridge.reply_owed and not bridge.active
        await bridge.forward(b"\x01\x02" * 800)
        assert not [c for c in conversation.calls if c[0] == "append"], (
            "narration from the mic was billed for a typed question"
        )
        await bridge._handle(AssistantAudio(pcm=REPLY_CHUNK, sample_rate=24_000, item_id="t"))
        await bridge._handle(TurnDone())
        states = []
        while not bridge.outbox.empty():
            event = bridge.outbox.get_nowait()
            if event.type == "session_state":
                states.append(event.model_dump()["state"])
        assert states == ["speaking", "ready"]

    asyncio.run(scenario())


def test_a_barge_in_nobody_speaks_into_closes_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet_voice import live as live_module  # noqa: PLC0415

    monkeypatch.setattr(live_module, "LISTEN_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        conversation = _Recording()
        bridge = _bridge(conversation)
        await bridge.engage("")
        await asyncio.sleep(0.15)
        assert not bridge.active and not bridge.reply_owed
        assert ("cancel", None) in conversation.calls
        last = None
        while not bridge.outbox.empty():
            last = bridge.outbox.get_nowait().model_dump()
        assert last is not None and last["state"] == "ready" and last["detail"] == "nothing heard"

        # And speech inside the window keeps it open.
        bridge2 = _bridge(_Recording())
        await bridge2.engage("")
        await bridge2._handle(SpeechStarted(audio_start_ms=100))
        await asyncio.sleep(0.15)
        assert bridge2.active
        await bridge2.aclose()

    asyncio.run(scenario())


def test_a_vendor_error_reaches_the_client_as_our_code_not_the_vendors() -> None:
    from motet_voice.realtime import ProviderError  # noqa: PLC0415

    async def scenario() -> dict[str, Any]:
        bridge = _bridge(_Recording())
        await bridge._handle(
            ProviderError(message="Cancellation failed: no active response", code="response_x")
        )
        return bridge.outbox.get_nowait().model_dump()

    event = asyncio.run(scenario())
    assert event["code"] == "provider_error"
    assert "response_x" not in event["message"] and "Cancellation" not in event["message"]
