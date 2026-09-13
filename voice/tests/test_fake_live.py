"""The fake-mode realtime arm, end to end through the real WebSocket — and the socket's
origin check.

``MOTET_VOICE_ARM=openai_realtime`` with ``MOTET_INFERENCE_MODE=fake`` is how the Play Live
loop is felt with no vendor and no spend: barge-in, the position reaching the model, a
streamed reply, resume. This drives exactly that loop as a browser would, over the socket,
from speech-shaped PCM rather than from a scripted vendor.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

import pytest
from fastapi.testclient import TestClient
from motet_voice.app import create_app, origin_allowed
from motet_voice.config import VoiceSettings
from motet_voice.realtime.fake_live import FakeLiveConversation
from starlette.websockets import WebSocketDisconnect

TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 20_000,
        "claims": [
            {"start_ms": 0, "end_ms": 10_000, "spoken_text": "Acme raised forty million."},
            {"start_ms": 10_000, "end_ms": 20_000, "spoken_text": "Example Ventures led."},
        ],
    }
]
BODY = {
    "persona": {"name": "Motet", "instructions": "Answer briefly."},
    "context": {"transcript": TRANSCRIPT, "spoken_through_ms": 15_000},
}


def _speech(ms: int, *, dbfs: float = -25.0) -> bytes:
    amplitude = math.sqrt(2) * 32768 * 10 ** (dbfs / 20)
    out = bytearray()
    for index in range(16_000 * ms // 1000):
        value = round(amplitude * math.sin(2 * math.pi * 300 * index / 16_000))
        out += int.to_bytes(value & 0xFFFF, 2, "little")
    return bytes(out)


def _fake_realtime(**extra: str) -> VoiceSettings:
    return VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_SESSION_SECRET": "test-secret-not-a-real-one",
            "MOTET_VOICE_ARM": "openai_realtime",
            **extra,
        }
    )


def _open(client: TestClient, **headers: str) -> Any:
    started = client.post("/v1/voice/sessions", json=BODY).json()
    socket = client.websocket_connect(
        f"/v1/voice/sessions/{started['session_id']}/stream", headers=headers
    )
    return started, socket


def test_the_whole_loop_runs_on_the_fake_live_arm_with_no_vendor() -> None:
    app = create_app(_fake_realtime())
    with TestClient(app) as client:
        health = client.get("/internal/health").json()
        assert health["arm"] == "openai_realtime" and health["arm_conversational"] is True
        started, connecting = _open(client)
        with connecting as socket:
            socket.send_text(
                json.dumps(
                    {"type": "authenticate", "token": started["session_token"], "config": BODY}
                )
            )
            ready = json.loads(socket.receive_text())
            assert ready["detail"].startswith("live conversation open")

            socket.send_text(json.dumps({"type": "narration_delivered", "duration_ms": 20_000}))
            socket.send_text(json.dumps({"type": "barge_in"}))
            interrupted = json.loads(socket.receive_text())
            assert interrupted["type"] == "interrupted_at"
            assert interrupted["context"]["segment_title"] == "Acme raises a Series B"
            assert interrupted["context"]["claim_text"] == "Example Ventures led."
            assert json.loads(socket.receive_text())["state"] == "listening"

            # A second of question, then quiet: the fake's end-of-utterance fires.
            for chunk in (_speech(1_000), bytes(16_000 * 2 * 7 // 10)):
                for offset in range(0, len(chunk), 3_200):
                    socket.send_bytes(chunk[offset : offset + 3_200])

            events: list[dict[str, Any]] = []
            while True:
                event = json.loads(socket.receive_text())
                events.append(event)
                if event["type"] == "session_state" and event["state"] == "ready":
                    break

            socket.send_text(json.dumps({"type": "narration_resumed", "spoken_through_ms": 15_000}))
            socket.send_text(json.dumps({"type": "close"}))
            assert json.loads(socket.receive_text())["state"] == "closed"

    user = [e for e in events if e["type"] == "transcript" and e["speaker"] == "user"]
    assert user and user[0]["text"].startswith("(fake transcript:")
    chunks = [e for e in events if e["type"] == "audio_chunk"]
    assert len(chunks) == 15
    assert all(c["format"] == "pcm16" and c["sample_rate"] == 24_000 for c in chunks)
    assert len(base64.b64decode(chunks[0]["pcm_base64"])) == 4_800
    assistant = [e for e in events if e["type"] == "transcript" and e["speaker"] == "assistant"]
    assert "You stopped me at 0:15, in 'Acme raises a Series B'." in assistant[0]["text"], (
        "the interruption position reached the (fake) model"
    )
    assert events[-1]["detail"] == "reply complete"


def test_the_fake_cancels_a_reply_the_listener_talks_over() -> None:
    import asyncio  # noqa: PLC0415

    async def run() -> list[Any]:
        fake = FakeLiveConversation()
        await fake.add_context(
            "The listener interrupted the briefing at 0:15, during the story 'X'."
        )
        await fake.add_user_text("what?")
        await asyncio.sleep(0.1)  # a couple of chunks out
        await fake.append_audio(_speech(200))
        await fake.aclose()
        return [event async for event in fake.events()]

    events = asyncio.run(run())
    names = [type(event).__name__ for event in events]
    assert "AssistantTranscript" not in names, "the cut reply never finished"
    assert names[-2:] == ["SpeechStarted", "TurnDone"]
    assert events[-1].cancelled


# ------------------------------------------------------------------------- origins


@pytest.mark.parametrize(
    ("configured", "origin", "allowed"),
    [
        ("", "https://evil.example", True),  # unset: a laptop, anything goes
        ("https://app.example", "https://app.example", True),
        ("https://app.example/", "HTTPS://APP.EXAMPLE", True),
        ("https://app.example", "https://evil.example", False),
        ("https://app.example", None, True),  # not a browser
    ],
)
def test_origin_allowed(configured: str, origin: str | None, allowed: bool) -> None:
    settings = _fake_realtime(MOTET_VOICE_ALLOWED_ORIGINS=configured)
    assert origin_allowed(settings, origin) is allowed


def test_a_socket_from_another_origin_is_refused_before_it_is_accepted() -> None:
    app = create_app(_fake_realtime(MOTET_VOICE_ALLOWED_ORIGINS="https://app.example"))
    with TestClient(app) as client:
        assert client.get("/internal/health").json()["origins_restricted"] is True
        _, connecting = _open(client, origin="https://evil.example")
        with pytest.raises(WebSocketDisconnect) as refused:
            with connecting:
                pass
        assert refused.value.code == 1008
        started, connecting = _open(client, origin="https://app.example")
        with connecting as socket:
            socket.send_text(
                json.dumps(
                    {"type": "authenticate", "token": started["session_token"], "config": BODY}
                )
            )
            assert json.loads(socket.receive_text())["state"] == "ready"
            socket.send_text(json.dumps({"type": "close"}))
