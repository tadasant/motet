"""Play Live's API half: ``GET /v1/voice`` and ``POST /v1/episodes/{id}/voice-session``.

Two properties matter most, and they point in opposite directions:

* **Unconfigured is the deployed state, and it must be clean.** No voice service exists in
  staging or production, so both routes have to answer "not configured" in the API's
  normal error shape — a 503 with a sentence, never a 500 — and the status route has to
  let the SPA decide not to offer the button at all.
* **Configured, the API is the one that assembles the context** (invariant 2). The voice
  service is exercised for real here — its own ``StartSession`` route, with its own start
  token check and its own config validation — behind an HTTP transport, so a context the
  voice service would reject cannot pass.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.main import voice_starter
from motet_api.voice import (
    NOT_CONFIGURED,
    VOICE_BASE_URL_ENV,
    VOICE_START_TOKEN_ENV,
    HttpVoiceStarter,
    websocket_url,
)
from motet_voice.app import create_app as create_voice_app
from motet_voice.config import VoiceSettings
from motet_workers import Queue, drain

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
START_TOKEN = "test-start-token"
VOICE_BASE = "https://voice.example.invalid"

NEWSLETTERS = [
    (
        "Acme raises $20M Series A",
        "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind "
        "Ventures, bringing total funding to $31M.",
    ),
    (
        "Regulator opens an inquiry",
        "Regulator opens an inquiry. The agency confirmed an inquiry into data retention "
        "practices at three large platforms.",
    ),
]


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    monkeypatch.setenv("MOTET_VAULT_BACKEND", "local")
    monkeypatch.delenv(VOICE_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(VOICE_START_TOKEN_ENV, raising=False)
    reset_store()
    with TestClient(app) as started:
        yield started
    app.dependency_overrides.pop(voice_starter, None)
    reset_store()


def rendered_episode(api: TestClient, url: str) -> dict[str, Any]:
    for item_title, text in NEWSLETTERS:
        pasted = api.post(
            "/v1/sources/paste", json={"title": item_title, "text": text}, headers=AUTH
        )
        assert pasted.status_code == 201
    drain(Queue.INTEGRATE, url)
    created = api.post(
        "/v1/episodes", json={"title": "Briefing", "max_duration_ms": 1_200_000}, headers=AUTH
    )
    assert created.status_code == 201
    for queue in (Queue.ASSEMBLE, Queue.SCRIPT, Queue.TTS):
        drain(queue, url)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "ready", episode.get("last_error")
    return episode


# ------------------------------------------------------------------ not configured


def test_with_no_voice_service_the_status_route_says_so(api: TestClient) -> None:
    status = api.get("/v1/voice", headers=AUTH)
    assert status.status_code == 200
    body = status.json()
    assert body["configured"] is False
    assert body["reason"].startswith(NOT_CONFIGURED)
    assert VOICE_BASE_URL_ENV in body["reason"] and VOICE_START_TOKEN_ENV in body["reason"]
    assert api.get("/internal/health").json()["voice_configured"] is False


def test_with_no_voice_service_minting_is_a_503_not_a_500(api: TestClient, _migrated: str) -> None:
    episode = rendered_episode(api, _migrated)
    minted = api.post(f"/v1/episodes/{episode['id']}/voice-session", json={}, headers=AUTH)
    assert minted.status_code == 503
    assert minted.json()["detail"].startswith(NOT_CONFIGURED)


def test_a_url_without_a_start_token_is_not_configured(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minting against an *open* voice service is not a configuration to accept."""
    monkeypatch.setenv(VOICE_BASE_URL_ENV, VOICE_BASE)
    body = api.get("/v1/voice", headers=AUTH).json()
    assert body["configured"] is False
    assert VOICE_START_TOKEN_ENV in body["reason"]
    assert VOICE_BASE_URL_ENV not in body["reason"]
    minted = api.post("/v1/episodes/ep_missing/voice-session", json={}, headers=AUTH)
    assert minted.status_code == 503


def test_the_status_route_needs_the_api_token(api: TestClient) -> None:
    assert api.get("/v1/voice").status_code == 401


# ------------------------------------------------------------------ configured


class VoiceBehindHttp:
    """The real voice service, reached through an httpx transport rather than a socket."""

    def __init__(self, *, start_token: str = START_TOKEN) -> None:
        settings = VoiceSettings.from_env(
            {
                "MOTET_INFERENCE_MODE": "fake",
                "MOTET_VOICE_SESSION_SECRET": "test-secret-not-a-real-one",
                "MOTET_VOICE_START_SESSION_TOKEN": start_token,
                "MOTET_VOICE_ARM": "openai_realtime",
            }
        )
        self.client = TestClient(create_voice_app(settings))
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answered = self.client.post(
            request.url.path,
            content=request.content,
            headers={
                "content-type": "application/json",
                "authorization": request.headers.get("authorization", ""),
            },
        )
        return httpx.Response(answered.status_code, content=answered.content)


def _configure(api: TestClient, monkeypatch: pytest.MonkeyPatch, voice: VoiceBehindHttp) -> None:
    monkeypatch.setenv(VOICE_BASE_URL_ENV, VOICE_BASE)
    monkeypatch.setenv(VOICE_START_TOKEN_ENV, START_TOKEN)
    app.dependency_overrides[voice_starter] = lambda: HttpVoiceStarter(
        VOICE_BASE, START_TOKEN, transport=httpx.MockTransport(voice.handle)
    )


def test_the_api_assembles_the_context_and_the_voice_service_accepts_it(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = VoiceBehindHttp()
    _configure(api, monkeypatch, voice)
    episode = rendered_episode(api, _migrated)

    assert api.get("/v1/voice", headers=AUTH).json() == {"configured": True, "reason": None}
    assert api.get("/internal/health").json()["voice_configured"] is True

    minted = api.post(
        f"/v1/episodes/{episode['id']}/voice-session",
        json={"spoken_through_ms": 1_500},
        headers=AUTH,
    )
    assert minted.status_code == 201, minted.text
    body = minted.json()

    # Server-to-server, with the start token — the browser never holds it.
    assert len(voice.requests) == 1
    assert voice.requests[0].headers["authorization"] == f"Bearer {START_TOKEN}"
    assert START_TOKEN not in minted.text

    assert body["websocket_url"].startswith("wss://voice.example.invalid/v1/voice/sessions/")
    assert body["websocket_url"].endswith("/stream")
    assert body["arm"] == "openai_realtime"
    frame = body["authenticate_frame"]
    assert frame["type"] == "authenticate"
    assert frame["token"] == body["session_token"]
    assert json.loads(voice.requests[0].content) == frame["config"], (
        "the config the client echoes must be the one the token was signed over"
    )

    context = frame["config"]["context"]
    assert context["episode_id"] == episode["id"]
    assert context["spoken_through_ms"] == 1_500
    assert [seg["title"] for seg in context["transcript"]] == [
        seg["news_item_title"] for seg in episode["segments"]
    ]
    claims = [claim for seg in context["transcript"] for claim in seg["claims"]]
    assert claims, "the timed transcript carries every claim"
    assert all(claim["end_ms"] > claim["start_ms"] for claim in claims), (
        "claims are timed from the server's apportioned timings, not re-derived"
    )
    assert [t["name"] for t in frame["config"]["tools"]] == ["mark_read"]


def test_the_minted_frame_opens_the_voice_socket(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole handshake: what the API returns is exactly what the socket accepts."""
    voice = VoiceBehindHttp()
    _configure(api, monkeypatch, voice)
    episode = rendered_episode(api, _migrated)
    body = api.post(f"/v1/episodes/{episode['id']}/voice-session", json={}, headers=AUTH).json()

    path = httpx.URL(body["websocket_url"]).path
    with voice.client.websocket_connect(path) as socket:
        socket.send_text(json.dumps(body["authenticate_frame"]))
        ready = json.loads(socket.receive_text())
        assert ready["type"] == "session_state" and ready["state"] == "ready"
        assert ready["detail"].startswith("live conversation open")
        socket.send_text(json.dumps({"type": "close"}))


def test_a_voice_service_that_refuses_the_start_token_is_a_503(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = VoiceBehindHttp(start_token="a-different-token")
    _configure(api, monkeypatch, voice)
    episode = rendered_episode(api, _migrated)
    minted = api.post(f"/v1/episodes/{episode['id']}/voice-session", json={}, headers=AUTH)
    assert minted.status_code == 503
    assert "start token" in minted.json()["detail"]


def test_a_voice_service_that_does_not_answer_is_a_503(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setenv(VOICE_BASE_URL_ENV, VOICE_BASE)
    monkeypatch.setenv(VOICE_START_TOKEN_ENV, START_TOKEN)
    app.dependency_overrides[voice_starter] = lambda: HttpVoiceStarter(
        VOICE_BASE, START_TOKEN, transport=httpx.MockTransport(refuse)
    )
    episode = rendered_episode(api, _migrated)
    minted = api.post(f"/v1/episodes/{episode['id']}/voice-session", json={}, headers=AUTH)
    assert minted.status_code == 503
    assert minted.json()["detail"] == "The voice service did not answer."
    assert "voice.example.invalid" not in minted.text, "the host is topology"


def test_an_unrendered_or_unknown_episode_is_refused_before_the_voice_service_is_asked(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice = VoiceBehindHttp()
    _configure(api, monkeypatch, voice)
    missing = api.post("/v1/episodes/ep_missing/voice-session", json={}, headers=AUTH)
    assert missing.status_code == 404
    api.post("/v1/sources/paste", json={"title": "t", "text": "Some text."}, headers=AUTH)
    pending = api.post(
        "/v1/episodes", json={"title": "B", "max_duration_ms": 600_000}, headers=AUTH
    ).json()
    refused = api.post(f"/v1/episodes/{pending['id']}/voice-session", json={}, headers=AUTH)
    assert refused.status_code == 409
    assert voice.requests == []


def test_claims_carry_their_timings_on_the_episode(api: TestClient, _migrated: str) -> None:
    episode = rendered_episode(api, _migrated)
    for segment in episode["segments"]:
        end = segment["start_ms"] + segment["duration_ms"]
        for claim in segment["claims"]:
            assert segment["start_ms"] <= claim["start_ms"] <= end
            assert claim["duration_ms"] > 0


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://voice.example", "wss://voice.example/v1/x"),
        ("http://localhost:8100/", "ws://localhost:8100/v1/x"),
    ],
)
def test_websocket_url(base: str, expected: str) -> None:
    assert websocket_url(base, "/v1/x") == expected


def test_minting_needs_the_api_token(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = VoiceBehindHttp()
    _configure(api, monkeypatch, voice)
    assert api.post("/v1/episodes/ep_x/voice-session", json={}).status_code == 401
    assert voice.requests == []


def test_an_unreadable_answer_from_the_voice_service_is_a_503_not_a_500(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"<html>not json</html>")

    monkeypatch.setenv(VOICE_BASE_URL_ENV, VOICE_BASE)
    monkeypatch.setenv(VOICE_START_TOKEN_ENV, START_TOKEN)
    app.dependency_overrides[voice_starter] = lambda: HttpVoiceStarter(
        VOICE_BASE, START_TOKEN, transport=httpx.MockTransport(garbled)
    )
    episode = rendered_episode(api, _migrated)
    minted = api.post(f"/v1/episodes/{episode['id']}/voice-session", json={}, headers=AUTH)
    assert minted.status_code == 503
    assert minted.json()["detail"] == "The voice service answered with something unreadable."
