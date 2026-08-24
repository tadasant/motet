"""The Phase 2 API surface, through ``TestClient``.

Sources and the OAuth handshake, smart episodes, highlights, playback progress, and the
subtitle and chapter documents — each exercised as a client meets it, so the dependency
graph, the response models, and the generated contract are all in the loop.

**The fake OAuth provider is what makes this possible.** The Google OAuth client does not
exist, so a test that needed one would not exist either; the fake completes consent
deterministically, and the credential it produces travels through the same vault path a
real token will.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_db import CredentialPurpose, phase2, repo
from motet_workers import Queue, drain

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REDIRECT = "https://app.example.invalid/oauth/callback"

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
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def run_pipeline(url: str) -> None:
    for queue in (Queue.INTEGRATE, Queue.ASSEMBLE, Queue.SCRIPT, Queue.TTS):
        drain(queue, url)


def paste_and_render(api: TestClient, url: str, title: str = "Briefing") -> dict[str, Any]:
    """Ingest two newsletters and render an episode, so there is real audio to caption."""
    for item_title, text in NEWSLETTERS:
        assert (
            api.post(
                "/v1/sources/paste",
                json={"title": item_title, "text": text},
                headers=AUTH,
            ).status_code
            == 201
        )
    drain(Queue.INTEGRATE, url)
    created = api.post(
        "/v1/episodes", json={"title": title, "max_duration_ms": 1_200_000}, headers=AUTH
    )
    assert created.status_code == 201
    run_pipeline(url)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "ready", episode.get("last_error")
    return episode


def connect_gmail(api: TestClient) -> str:
    """Walk the whole consent flow against the fake provider."""
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    assert started.status_code == 201, started.text
    body = started.json()
    done = api.post(
        "/v1/sources/callback",
        json={"state": body["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    assert done.status_code == 200, done.text
    assert done.json()["connected"] is True
    return str(body["source_id"])


# --- sources and the OAuth handshake -------------------------------------------------


def test_connecting_a_mailbox_seals_a_credential_and_queues_a_poll(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id = connect_gmail(api)

    stored = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert stored is not None
    assert stored.backend == "local", "the fake backend, because no keyring is provisioned"

    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM jobs WHERE queue = %s AND state = 'ready'",
            (Queue.POLL.value,),
        )
        row = cur.fetchone()
    assert row is not None and row["n"] == 1, "connecting should start ingesting"


def test_the_credential_never_appears_in_a_response(api: TestClient) -> None:
    """The API seals a token and then must never hand it back.

    Not even to the owner: nothing downstream needs it, and a token in a JSON body is a
    token in a browser's network log.
    """
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    done = api.post(
        "/v1/sources/callback",
        json={"state": started.json()["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    rendered = json.dumps(done.json())
    assert "fake-refresh" not in rendered
    assert "fake-access" not in rendered
    for body in (rendered, json.dumps(api.get("/v1/sources", headers=AUTH).json())):
        assert "token" not in body.lower() or "api token" in body.lower()


def test_a_replayed_callback_is_refused(api: TestClient) -> None:
    """The state is single-use, so an intercepted redirect cannot be redeemed twice."""
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    state = started.json()["state"]
    assert (
        api.post(
            "/v1/sources/callback", json={"state": state, "code": "c"}, headers=AUTH
        ).status_code
        == 200
    )
    replayed = api.post("/v1/sources/callback", json={"state": state, "code": "c"}, headers=AUTH)
    assert replayed.status_code == 400
    assert "already used" in replayed.json()["detail"]


def test_an_unknown_state_is_refused(api: TestClient) -> None:
    refused = api.post("/v1/sources/callback", json={"state": "st_nope", "code": "c"}, headers=AUTH)
    assert refused.status_code == 400


def test_the_authorization_url_carries_pkce_and_offline_access(api: TestClient) -> None:
    """Three parameters, each of which breaks the connection if missing.

    Without `access_type=offline` Google issues no refresh token; without `prompt=consent`
    a re-connect gets no refresh token either; without PKCE an intercepted code is
    redeemable.
    """
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    url = started.json()["authorization_url"]
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "gmail.readonly" in url


def test_a_source_starts_inactive_until_consent_completes(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """A source with no token would be polled and fail on every run."""
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    source_id = started.json()["source_id"]
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active
    assert phase2.list_pollable_sources(db, "gmail") == []


def test_x_bookmarks_are_refused_by_name(api: TestClient) -> None:
    """Not built: the API tier is a spend decision nobody has made."""
    refused = api.post(
        "/v1/sources/connect",
        json={"provider": "x", "name": "X", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    assert refused.status_code == 400
    assert "gmail" in refused.json()["detail"]


def test_listing_sources_reports_connection_without_decrypting(api: TestClient) -> None:
    source_id = connect_gmail(api)
    listed = api.get("/v1/sources", headers=AUTH).json()
    gmail = next(source for source in listed if source["id"] == source_id)
    assert gmail["connected"] is True
    assert gmail["active"] is True
    assert "gmail.readonly" in gmail["scopes"][0]
    # The Phase 1 paste source is listed too, and has no credential.
    paste = next(source for source in listed if source["id"] == repo.PASTE_SOURCE_ID)
    assert paste["connected"] is False


def test_disconnecting_forgets_the_credential_and_stops_polling(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id = connect_gmail(api)
    assert api.delete(f"/v1/sources/{source_id}/credentials", headers=AUTH).status_code == 204
    assert (
        phase2.get_source_credential(
            db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
        )
        is None
    )
    source = phase2.get_source(db, source_id)
    assert source is not None and not source.active
    # Refused rather than silently queued: a disconnected mailbox cannot be polled.
    assert api.post(f"/v1/sources/{source_id}/poll", headers=AUTH).status_code == 409


def test_polling_can_be_triggered_by_hand(api: TestClient) -> None:
    source_id = connect_gmail(api)
    assert api.post(f"/v1/sources/{source_id}/poll", headers=AUTH).status_code == 200
    assert api.post("/v1/sources/nope/poll", headers=AUTH).status_code == 404


def test_the_phase_2_routes_require_authentication(api: TestClient) -> None:
    """Every new route is behind the same lock as the Phase 1 ones."""
    for method, path, body in (
        ("get", "/v1/sources", None),
        ("post", "/v1/sources/connect", {"redirect_uri": REDIRECT}),
        ("post", "/v1/sources/callback", {"state": "s", "code": "c"}),
        ("get", "/v1/highlights", None),
        ("post", "/v1/highlights", {}),
        ("post", "/v1/episodes/smart", {"title": "t", "max_duration_ms": 1}),
        ("post", "/v1/episodes/ep_x/progress", {"listened_through_ms": 0}),
    ):
        response = (
            getattr(api, method)(path, json=body)
            if body is not None
            else getattr(api, method)(path)
        )
        assert response.status_code == 401, f"{method} {path} was not behind the token"


# --- smart episodes ------------------------------------------------------------------


def test_a_smart_episode_is_created_with_its_rule(api: TestClient, _migrated: str) -> None:
    for title, text in NEWSLETTERS:
        api.post("/v1/sources/paste", json={"title": title, "text": text}, headers=AUTH)
    drain(Queue.INTEGRATE, _migrated)

    created = api.post(
        "/v1/episodes/smart",
        json={
            "title": "Morning briefing",
            "max_duration_ms": 1_200_000,
            "rule": {"ranking": "coverage", "window_days": 7},
        },
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    run_pipeline(_migrated)

    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "ready", episode.get("last_error")
    assert episode["segments"], "a smart episode should have selected something"


def test_a_bad_ranking_is_refused_at_creation(api: TestClient) -> None:
    """Validated where the mistake is made, not minutes later on a queue."""
    refused = api.post(
        "/v1/episodes/smart",
        json={
            "title": "Briefing",
            "max_duration_ms": 600_000,
            "rule": {"ranking": "by_vibes"},
        },
        headers=AUTH,
    )
    assert refused.status_code == 422
    assert "ranking must be one of" in refused.text


def test_a_window_beyond_the_ceiling_is_refused(api: TestClient) -> None:
    refused = api.post(
        "/v1/episodes/smart",
        json={"title": "B", "max_duration_ms": 600_000, "rule": {"window_days": 400}},
        headers=AUTH,
    )
    assert refused.status_code == 422


def test_a_smart_episode_with_nothing_selected_fails_visibly(
    api: TestClient, _migrated: str
) -> None:
    """Permanent, not retried: an empty backlog is still empty in ten minutes."""
    created = api.post(
        "/v1/episodes/smart",
        json={"title": "Nothing", "max_duration_ms": 600_000, "rule": {"window_days": 1}},
        headers=AUTH,
    )
    assert created.status_code == 201
    drain(Queue.ASSEMBLE, _migrated)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "failed"
    assert "rule" in (episode["last_error"] or "")


# --- read state from the audio side --------------------------------------------------


def test_reporting_progress_marks_what_was_passed(api: TestClient, _migrated: str) -> None:
    """Invariant 5, from the audio side, over HTTP."""
    episode = paste_and_render(api, _migrated)
    assert len(episode["segments"]) >= 2

    first_end = episode["segments"][0]["start_ms"] + episode["segments"][0]["duration_ms"]

    midway = api.post(
        f"/v1/episodes/{episode['id']}/progress",
        json={"listened_through_ms": max(0, first_end - 1)},
        headers=AUTH,
    ).json()
    assert midway["news_items_marked_read"] == 0

    past_first = api.post(
        f"/v1/episodes/{episode['id']}/progress",
        json={"listened_through_ms": first_end},
        headers=AUTH,
    ).json()
    assert past_first["news_items_marked_read"] == 1

    backlog = api.get("/v1/news-items", headers=AUTH).json()
    read = [item for item in backlog if item["read"]]
    assert len(read) == 1, "the visual surface sees the same fact"


def test_progress_does_not_go_backwards_over_http(api: TestClient, _migrated: str) -> None:
    episode = paste_and_render(api, _migrated)
    api.post(
        f"/v1/episodes/{episode['id']}/progress",
        json={"listened_through_ms": episode["duration_ms"]},
        headers=AUTH,
    )
    rewound = api.post(
        f"/v1/episodes/{episode['id']}/progress",
        json={"listened_through_ms": 0},
        headers=AUTH,
    ).json()
    assert rewound["listened_through_ms"] == episode["duration_ms"]
    assert rewound["news_items_marked_read"] == 0


def test_progress_on_an_unknown_episode_is_a_404(api: TestClient) -> None:
    assert (
        api.post(
            "/v1/episodes/ep_nope/progress",
            json={"listened_through_ms": 1},
            headers=AUTH,
        ).status_code
        == 404
    )


def test_a_negative_position_is_refused_by_the_contract(api: TestClient) -> None:
    assert (
        api.post(
            "/v1/episodes/ep_x/progress",
            json={"listened_through_ms": -1},
            headers=AUTH,
        ).status_code
        == 422
    )


# --- highlights ----------------------------------------------------------------------


def test_a_highlight_quotes_the_source_not_the_caller(api: TestClient, _migrated: str) -> None:
    """The trust property, over HTTP.

    The request carries no quote at all — only a span — so a model calling this tool
    cannot write its paraphrase in and have it look verbatim.
    """
    episode = paste_and_render(api, _migrated)
    claim = episode["segments"][0]["claims"][0]

    saved = api.post(
        "/v1/highlights",
        json={
            "news_item_id": episode["segments"][0]["news_item_id"],
            "source_item_id": claim["span"]["source_item_id"],
            "span_start": claim["span"]["start"],
            "span_end": claim["span"]["end"],
            "note": "check this",
            "episode_id": episode["id"],
            "anchor_ms": 1_500,
        },
        headers=AUTH,
    )
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["quote"] == claim["source_excerpt"]
    assert body["note"] == "check this"
    assert body["episode_id"] == episode["id"]
    assert body["anchor_ms"] == 1_500


def test_a_highlight_with_an_unresolvable_span_is_refused(api: TestClient, _migrated: str) -> None:
    episode = paste_and_render(api, _migrated)
    claim = episode["segments"][0]["claims"][0]
    refused = api.post(
        "/v1/highlights",
        json={
            "news_item_id": episode["segments"][0]["news_item_id"],
            "source_item_id": claim["span"]["source_item_id"],
            "span_start": 0,
            "span_end": 99_999,
        },
        headers=AUTH,
    )
    assert refused.status_code == 422
    assert "anchor" in refused.json()["detail"]


def test_an_inverted_span_is_refused(api: TestClient) -> None:
    refused = api.post(
        "/v1/highlights",
        json={
            "news_item_id": "ni_x",
            "source_item_id": "si_x",
            "span_start": 10,
            "span_end": 4,
        },
        headers=AUTH,
    )
    assert refused.status_code == 422


def test_highlights_list_and_delete(api: TestClient, _migrated: str) -> None:
    episode = paste_and_render(api, _migrated)
    claim = episode["segments"][0]["claims"][0]
    saved = api.post(
        "/v1/highlights",
        json={
            "news_item_id": episode["segments"][0]["news_item_id"],
            "source_item_id": claim["span"]["source_item_id"],
            "span_start": claim["span"]["start"],
            "span_end": claim["span"]["end"],
        },
        headers=AUTH,
    ).json()

    assert len(api.get("/v1/highlights", headers=AUTH).json()) == 1
    assert api.delete(f"/v1/highlights/{saved['id']}", headers=AUTH).status_code == 204
    assert api.get("/v1/highlights", headers=AUTH).json() == []
    assert api.delete(f"/v1/highlights/{saved['id']}", headers=AUTH).status_code == 404


# --- subtitles and chapters ----------------------------------------------------------


def test_the_transcript_is_valid_webvtt(api: TestClient, _migrated: str) -> None:
    """Header, blank line, and `HH:MM:SS.mmm` cues.

    A byte-order mark or a missing blank line makes some parsers reject the file outright,
    and the failure mode is a transcript button that does nothing.
    """
    episode = paste_and_render(api, _migrated)
    feed = api.get("/v1/feed", headers=AUTH).json()
    response = api.get(
        f"/v1/episodes/{episode['id']}/transcript.vtt", params={"token": feed["token"]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")

    body = response.text
    assert body.startswith("WEBVTT\n\n"), repr(body[:40])
    assert "﻿" not in body

    cues = [line for line in body.split("\n") if "-->" in line]
    assert cues, "an episode with claims should produce cues"
    for cue in cues:
        start, end = (part.strip() for part in cue.split("-->"))
        for stamp in (start, end):
            hours, minutes, rest = stamp.split(":")
            seconds, millis = rest.split(".")
            assert len(hours) == 2 and len(minutes) == 2
            assert len(seconds) == 2 and len(millis) == 3
        assert start < end, f"a zero-length cue would flash and vanish: {cue}"


def test_the_transcript_cues_are_in_order_and_inside_the_episode(
    api: TestClient, _migrated: str
) -> None:
    """Timing comes from claims apportioned within measured segments.

    Cues that overlapped or ran past the audio would desynchronize a caption track from the
    thing it is captioning, which is the whole point of having one.
    """
    episode = paste_and_render(api, _migrated)
    feed = api.get("/v1/feed", headers=AUTH).json()
    body = api.get(
        f"/v1/episodes/{episode['id']}/transcript.vtt", params={"token": feed["token"]}
    ).text

    previous_end = "00:00:00.000"
    for cue in (line for line in body.split("\n") if "-->" in line):
        start, end = (part.strip() for part in cue.split("-->"))
        assert start >= previous_end, "cues must not overlap"
        previous_end = end
    total = episode["duration_ms"]
    hours, minutes, rest = previous_end.split(":")
    seconds, millis = rest.split(".")
    last_ms = int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1_000 + int(millis)
    assert last_ms <= total + 1, "the last cue must not run past the audio"


def test_the_chapters_document_is_podcasting_2_0_shaped(api: TestClient, _migrated: str) -> None:
    """`startTime` is in **seconds** — the one unit change, and the one silent mis-render."""
    episode = paste_and_render(api, _migrated)
    feed = api.get("/v1/feed", headers=AUTH).json()
    response = api.get(
        f"/v1/episodes/{episode['id']}/chapters.json", params={"token": feed["token"]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json+chapters")

    document = json.loads(response.text)
    assert document["version"] == "1.2.0"
    chapters = document["chapters"]
    assert len(chapters) == len(episode["segments"])
    assert chapters[0]["startTime"] == 0
    assert [c["startTime"] for c in chapters] == sorted(c["startTime"] for c in chapters)
    for chapter, segment in zip(chapters, episode["segments"], strict=True):
        assert chapter["title"] == segment["news_item_title"]
        assert chapter["startTime"] == pytest.approx(segment["start_ms"] / 1000, abs=0.01)


def test_the_side_documents_use_the_feed_token(api: TestClient, _migrated: str) -> None:
    """A podcast client sends the credential it found in the tag, not an API token."""
    episode = paste_and_render(api, _migrated)
    for asset in ("transcript.vtt", "chapters.json"):
        assert api.get(f"/v1/episodes/{episode['id']}/{asset}").status_code == 401
        assert (
            api.get(f"/v1/episodes/{episode['id']}/{asset}", params={"token": "wrong"}).status_code
            == 401
        )


def test_an_unrendered_episode_has_no_transcript(api: TestClient) -> None:
    """Before TTS, every claim's timing is zero.

    An absent document reads as "not available yet"; a stack of cues at 00:00 reads as
    broken, and a client would cache it.
    """
    created = api.post(
        "/v1/episodes", json={"title": "Pending", "max_duration_ms": 600_000}, headers=AUTH
    )
    feed = api.get("/v1/feed", headers=AUTH).json()
    assert (
        api.get(
            f"/v1/episodes/{created.json()['id']}/transcript.vtt",
            params={"token": feed["token"]},
        ).status_code
        == 404
    )
