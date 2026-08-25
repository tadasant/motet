"""The service contract over HTTP and a WebSocket — with a fake arm, on Cloud Run's terms."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from motet_voice.app import HEALTH_PATH, PLATFORM_RESERVED_PATHS, _abandon, create_app
from motet_voice.config import VoiceSettings
from motet_voice.harness import synthesize_walk
from motet_voice.realtime import build_composed_arm
from motet_voice.tools import RecordingToolTransport, ToolResponse

PERSONA = {"name": "Briefing", "instructions": "Be brief.", "voice": "narrator"}


@dataclass
class _ScriptedModel:
    """A conversation leg that says exactly what a test wants said."""

    text: str

    @property
    def name(self) -> str:
        return "scripted"

    def reply(self, request: Any, user_text: str) -> str:
        return self.text


def _scripted_arm(settings: VoiceSettings, reply: str) -> Any:
    arm = build_composed_arm(settings)
    return replace(arm, model=_ScriptedModel(reply))


@pytest.fixture
def transport() -> RecordingToolTransport:
    return RecordingToolTransport(
        responses={"POST /v1/news-items/n1/read": ToolResponse(200, {"id": "n1", "read": True})}
    )


@pytest.fixture
def client(settings: VoiceSettings, transport: RecordingToolTransport) -> Any:
    app = create_app(settings, arm=build_composed_arm(settings), transport=transport)
    with TestClient(app) as test_client:
        yield test_client


def _start(client: Any, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"persona": PERSONA, **overrides}
    response = client.post("/v1/voice/sessions", json=body)
    assert response.status_code == 201, response.text
    return {"body": body, "response": response.json()}


def test_health_is_not_on_a_path_the_platform_reserves(client: Any) -> None:
    """The same collision the API hit — motet#16 — on the same platform.

    Cloud Run's frontend answers ``/healthz`` with its own 404 before the request reaches
    the container, so a health endpoint served there is unreadable from everywhere health
    is actually checked. This service is not deployed yet, which is exactly why the guard
    belongs here now: the mistake is cheapest to not make.

    A container-local check cannot observe the frontend — there isn't one here. What it can
    do is fail the moment the path is reintroduced.
    """
    assert HEALTH_PATH == "/internal/health"
    for reserved in PLATFORM_RESERVED_PATHS:
        assert not any(
            route.path == reserved or route.path.startswith(reserved + "/")
            for route in client.app.routes
            if hasattr(route, "path")
        )
    assert client.get("/healthz").status_code == 404


def test_health_reports_what_is_dormant(client: Any) -> None:
    payload = client.get(HEALTH_PATH).json()
    assert payload["status"] == "ok"
    assert payload["service"] == "motet-voice"
    assert payload["arm"] == "composed"
    assert payload["session_secret_configured"] is True
    assert {tool["name"] for tool in payload["tools"]} == {
        "save_highlight",
        "get_item_detail",
        "start_research",
        "mark_read",
    }
    dormant = {tool["name"] for tool in payload["tools"] if tool["state"] == "dormant"}
    assert "start_research" in dormant


def test_health_names_the_grounding_checker_and_says_it_does_not_gate(client: Any) -> None:
    """An operator has to be able to see, without reading code, which half of motet#10 runs.

    ``grounding_advisory`` is stated rather than inferred: invariant 3's hard gate lives on
    the narration path, and a reader of this route must not assume it applies here too.
    """
    payload = client.get(HEALTH_PATH).json()
    assert payload["grounding_checker"] == "specifics"
    assert payload["grounding_advisory"] is True
    # Configured and exporting are different questions; with no OTLP endpoint set, the
    # second is false and the counters go nowhere. Saying so is the point of the field.
    assert payload["telemetry_configured"] is False
    assert payload["telemetry_exporting"] is False


def test_an_ungrounded_reply_reaches_the_client_over_the_wire(settings: VoiceSettings) -> None:
    """End to end on the socket: the audio goes out, and the verdict follows it.

    The advisory half of motet#10 as a client sees it — not as a unit test of the checker.
    """
    app = create_app(settings, arm=_scripted_arm(settings, "Sequoia led the 900 million round."))
    with TestClient(app) as client:
        with _authenticated(client, context={"notes": "Helion raised 425 million."}) as socket:
            socket.send_text(json.dumps({"type": "text", "text": "who led it"}))
            events = [json.loads(socket.receive_text()) for _ in range(4)]

    assert events[2]["type"] == "audio_chunk", "it was spoken; the check did not gate it"
    verdict = events[3]
    assert verdict["type"] == "grounding"
    assert verdict["grounded"] is False
    assert verdict["checker"] == "specifics"
    assert {item["kind"] for item in verdict["unsupported"]} == {"name", "number"}
    assert verdict["reply"] == "Sequoia led the 900 million round."


def test_the_closed_frame_is_the_last_one_a_client_sees(settings: VoiceSettings) -> None:
    """A client tears down on ``closed``, so a verdict queued after it is a verdict lost."""
    app = create_app(settings, arm=_scripted_arm(settings, "Sequoia led the 900 million round."))
    with TestClient(app) as client:
        with _authenticated(client) as socket:
            socket.send_text(json.dumps({"type": "text", "text": "who led it"}))
            for _ in range(3):
                socket.receive_text()
            socket.send_text(json.dumps({"type": "close"}))
            tail = [json.loads(socket.receive_text()) for _ in range(2)]

    assert [event["type"] for event in tail] == ["grounding", "session_state"]
    assert tail[1]["state"] == "closed"


def test_abandoning_the_outbox_releases_join_rather_than_waiting_out_the_flush() -> None:
    """Without this, every disconnect with a backlog costs the full flush timeout."""

    async def scenario() -> None:
        outbox: asyncio.Queue[Any] = asyncio.Queue()
        for _ in range(3):
            outbox.put_nowait(object())
        _abandon(outbox)
        assert outbox.empty()
        # `join()` returning at all is the assertion: it blocks until every queued item
        # has been marked done, which is precisely what `_abandon` is for.
        await asyncio.wait_for(outbox.join(), timeout=1)

    asyncio.run(scenario())


def test_start_session_can_require_a_bearer(settings: VoiceSettings) -> None:
    """A session is a capability: its tools carry *this service's* credential for the API.

    So an open ``StartSession`` is a confused deputy with a read path into the corpus, not
    merely a way to spend inference budget.
    """
    guarded = VoiceSettings.from_env(
        {
            "MOTET_INFERENCE_MODE": "fake",
            "MOTET_VOICE_SESSION_SECRET": "test-secret-not-a-real-one",
            "MOTET_VOICE_START_SESSION_TOKEN": "a-token-for-a-unit-test",
        }
    )
    app = create_app(guarded, arm=build_composed_arm(guarded), transport=RecordingToolTransport())
    with TestClient(app) as guarded_client:
        assert guarded_client.get(HEALTH_PATH).json()["start_session_authenticated"] is True

        body = {"persona": PERSONA}
        assert guarded_client.post("/v1/voice/sessions", json=body).status_code == 401
        assert (
            guarded_client.post(
                "/v1/voice/sessions", json=body, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            guarded_client.post(
                "/v1/voice/sessions",
                json=body,
                headers={"Authorization": "Bearer a-token-for-a-unit-test"},
            ).status_code
            == 201
        )


def test_an_unset_start_session_token_is_reported_as_open(client: Any) -> None:
    """Open is a legitimate local default, and an undetectable one is the problem."""
    assert client.get(HEALTH_PATH).json()["start_session_authenticated"] is False


def test_start_session_returns_a_token_and_a_socket_path(client: Any) -> None:
    started = _start(client)["response"]
    assert started["session_token"].startswith("v1.")
    assert started["websocket_path"].endswith("/stream")
    assert started["session_id"] in started["websocket_path"]
    assert started["arm"] == "composed"


def test_an_unknown_tool_is_refused_at_start_session(client: Any) -> None:
    response = client.post(
        "/v1/voice/sessions",
        json={"persona": PERSONA, "tools": [{"name": "launch_the_missiles"}]},
    )
    assert response.status_code == 422
    assert "launch_the_missiles" in response.text


def test_mcp_servers_are_part_of_the_contract_but_resolve_to_nothing_yet(client: Any) -> None:
    response = client.post(
        "/v1/voice/sessions",
        json={"persona": PERSONA, "mcp_servers": [{"name": "x", "slug": "y"}]},
    )
    assert response.status_code == 422


def test_a_socket_needs_a_token_that_matches_its_config(client: Any) -> None:
    """The token's digest binds the config, which is what makes a stateless service safe."""
    started = _start(client)
    session_id = started["response"]["session_id"]

    with client.websocket_connect(f"/v1/voice/sessions/{session_id}/stream") as socket:
        socket.send_text(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": started["response"]["session_token"],
                    "config": {
                        "persona": PERSONA,
                        "tools": [{"name": "mark_read"}],  # not what was signed
                    },
                }
            )
        )
        event = json.loads(socket.receive_text())
    assert event["code"] == "unauthorized"
    assert "different session config" in event["message"]


def test_a_forged_token_is_rejected(client: Any) -> None:
    started = _start(client)
    session_id = started["response"]["session_id"]
    with client.websocket_connect(f"/v1/voice/sessions/{session_id}/stream") as socket:
        socket.send_text(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": "v1.x.9999999999.abc.sig",
                    "config": started["body"],
                }
            )
        )
        event = json.loads(socket.receive_text())
    assert event["code"] == "unauthorized"


@contextmanager
def _authenticated(client: Any, **overrides: Any) -> Iterator[Any]:
    """Open an authenticated socket and — importantly — close it again.

    A context manager rather than a bare helper because a socket left open holds the
    server task blocked on ``receive()``, and ``TestClient.__exit__`` then waits for it
    forever. A test that hangs is worse than a test that fails.
    """
    started = _start(client, **overrides)
    session_id = started["response"]["session_id"]
    with client.websocket_connect(f"/v1/voice/sessions/{session_id}/stream") as socket:
        socket.send_text(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": started["response"]["session_token"],
                    "config": started["body"],
                }
            )
        )
        ready = json.loads(socket.receive_text())
        assert ready["state"] == "ready"
        try:
            yield socket
        finally:
            socket.close()


def test_a_turn_produces_transcripts_and_audio(client: Any) -> None:
    """...and the grounding verdict arrives *behind* the audio, which is what advisory means.

    The order in this assertion is the decision in motet#10, expressed as a wire fact: the
    listener has the answer in their ear before anything has judged it. On the narration
    path the same check is a gate and the order is the other way round.
    """
    with _authenticated(client, tools=[{"name": "mark_read", "defaults": {}}]) as socket:
        socket.send_text(json.dumps({"type": "text", "text": "what was that"}))
        events = [json.loads(socket.receive_text()) for _ in range(4)]
        socket.send_text(json.dumps({"type": "close"}))
        assert json.loads(socket.receive_text())["state"] == "closed"

    assert [event["type"] for event in events] == [
        "transcript",
        "transcript",
        "audio_chunk",
        "grounding",
    ]
    assert events[0]["speaker"] == "user"
    assert events[1]["speaker"] == "assistant"
    assert events[2]["duration_ms"] > 0
    assert events[3]["grounded"] is True
    assert events[3]["checker"] == "specifics"
    assert events[3]["reply"] == events[1]["text"]


def test_an_assistant_reply_does_not_advance_the_narration_clock(client: Any) -> None:
    """The assistant talking is not the briefing playing. Conflating them marks stories read.

    ``spoken_through_ms`` is a position in the *episode*. A conversational answer is audio too,
    and folding its length into that position would advance read state by however long the
    assistant spoke for.
    """
    with _authenticated(client, context={"spoken_through_ms": 30_000}) as socket:
        socket.send_text(json.dumps({"type": "text", "text": "go on"}))
        for _ in range(4):  # transcript, transcript, audio_chunk, grounding
            socket.receive_text()
        socket.send_text(json.dumps({"type": "barge_in"}))
        assert json.loads(socket.receive_text())["offset_ms"] == 30_000


def test_audio_frames_produce_interrupted_at_with_our_own_offset(client: Any) -> None:
    """`interrupted_at(offset)` — and the offset is ours, not any provider's (invariant 4)."""
    walk = synthesize_walk(duration_ms=10_000, speech_at_ms=(3_000,), speech_duration_ms=2_000)
    with _authenticated(
        client,
        turn_policy={"mode": "open_mic", "require_narration_playing": False},
        context={"spoken_through_ms": 42_000},
    ) as socket:
        socket.send_bytes(walk.pcm)
        event = json.loads(socket.receive_text())

    assert event["type"] == "interrupted_at"
    assert event["offset_ms"] == 42_000, "the offset comes from our clock, not from the audio"
    assert event["decision"]["trigger"].startswith("local_vad")
    assert "snr_db" in event["decision"], "the evidence travels with the interruption"


def test_streamed_audio_produces_one_barge_in_per_utterance(client: Any) -> None:
    """Audio arrives in packets, and a frame's offset is into the *session*, not the packet.

    The bug this guards against was invisible to every other test in this file, because they
    all send a whole recording in one message. Numbering each packet's frames from zero gives
    every frame an offset near zero, and the refractory window compares the current frame's
    end against the last decision — so the detector fires once and then believes itself
    permanently inside its own cooldown for the rest of the walk.
    """
    walk = synthesize_walk(
        duration_ms=30_000, speech_at_ms=(6_000, 16_000, 26_000), speech_duration_ms=1_500
    )
    chunk = 16_000 * 2 // 5  # 200 ms of PCM16, the sort of packet a client actually sends

    events = []
    with _authenticated(
        client, turn_policy={"mode": "open_mic", "require_narration_playing": False}
    ) as socket:
        for offset in range(0, len(walk.pcm), chunk):
            socket.send_bytes(walk.pcm[offset : offset + chunk])
        socket.send_text(json.dumps({"type": "close"}))
        while True:
            event = json.loads(socket.receive_text())
            if event["type"] == "session_state" and event["state"] == "closed":
                break
            events.append(event)

    barge_ins = [event for event in events if event["type"] == "interrupted_at"]
    assert len(barge_ins) >= 2, (
        f"streaming three utterances produced {len(barge_ins)} barge-in(s); the detector is "
        "stuck in its own refractory window"
    )
    assert barge_ins[0]["decision"]["at_ms"] > 1_000, (
        "a decision's offset must be into the session, not into whichever packet carried it"
    )
    assert [b["decision"]["at_ms"] for b in barge_ins] == sorted(
        b["decision"]["at_ms"] for b in barge_ins
    )


def test_a_session_does_not_close_the_process_wide_arm(
    client: Any, transport: RecordingToolTransport
) -> None:
    """Cloud Run serves many sessions per instance; one hanging up must not close the arm."""
    voice_app = client.app.state.voice
    with _authenticated(client) as socket:
        socket.send_text(json.dumps({"type": "close"}))
        json.loads(socket.receive_text())

    assert voice_app.arm.capabilities().name == "composed"
    with _authenticated(client) as socket:
        socket.send_text(json.dumps({"type": "text", "text": "still here?"}))
        assert json.loads(socket.receive_text())["type"] == "transcript"


def test_a_provider_reported_position_does_not_move_the_clock(client: Any) -> None:
    with _authenticated(client, context={"spoken_through_ms": 10_000}) as socket:
        socket.send_text(json.dumps({"type": "provider_position", "spoken_through_ms": 99_000}))
        socket.send_text(json.dumps({"type": "barge_in"}))
        event = json.loads(socket.receive_text())

    assert event["type"] == "interrupted_at"
    assert event["offset_ms"] == 10_000


def test_a_client_reported_position_does_move_it(client: Any) -> None:
    with _authenticated(client, context={"spoken_through_ms": 10_000}) as socket:
        socket.send_text(json.dumps({"type": "playback_position", "spoken_through_ms": 55_000}))
        socket.send_text(json.dumps({"type": "barge_in"}))
        assert json.loads(socket.receive_text())["offset_ms"] == 55_000


def test_push_to_talk_ignores_streamed_audio(client: Any) -> None:
    noisy = synthesize_walk(duration_ms=5_000, speech_at_ms=(1_000,)).pcm
    with _authenticated(client, turn_policy={"mode": "push_to_talk"}) as socket:
        socket.send_bytes(noisy)
        socket.send_text(json.dumps({"type": "close"}))
        assert json.loads(socket.receive_text())["state"] == "closed"


def test_one_bad_frame_does_not_end_the_session(client: Any) -> None:
    """A client that sends one malformed message should not lose a walk."""
    with _authenticated(client) as socket:
        socket.send_text("{not json")
        assert json.loads(socket.receive_text())["code"] == "bad_json"
        socket.send_text(json.dumps({"type": "nonsense"}))
        assert json.loads(socket.receive_text())["code"] == "unknown_frame"
        socket.send_text(json.dumps({"type": "close"}))
        assert json.loads(socket.receive_text())["state"] == "closed"


def test_an_ephemeral_session_secret_is_reported_on_health() -> None:
    settings = VoiceSettings.from_env({"MOTET_INFERENCE_MODE": "fake"})
    app = create_app(settings, arm=build_composed_arm(settings), transport=RecordingToolTransport())
    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).json()["session_secret_configured"] is False
