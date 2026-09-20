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
from motet_api.deps import dek_wrapper, reset_store
from motet_db import CredentialPurpose, phase2, repo
from motet_sources import DEFAULT_QUERY
from motet_vault import BACKEND_ENV, KMS_KEY_ENV, CloudKmsKeyManager
from motet_workers import RESYNC_REQUESTED_CONFIG_KEY, Queue, drain

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


def test_the_first_sync_window_is_chosen_at_connect_and_reported_back(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """motet#139: the window a connect screen showed is the window the mailbox gets.

    It used to be a constant in the worker that no screen mentioned, so a first sync that
    stopped at seven days read as a pagination bug rather than as the cap it was.
    """
    started = api.post(
        "/v1/sources/connect",
        json={
            "provider": "gmail",
            "name": "Gmail",
            "redirect_uri": REDIRECT,
            "first_sync_days": 90,
        },
        headers=AUTH,
    )
    assert started.status_code == 201, started.text
    source_id = started.json()["source_id"]

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.config["first_sync_days"] == 90, "on the row before consent completes"

    api.post(
        "/v1/sources/callback",
        json={"state": started.json()["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    listed = {row["id"]: row for row in api.get("/v1/sources", headers=AUTH).json()}
    assert listed[source_id]["configured_first_sync_days"] == 90
    # What the *last* first sync reached is a different fact and is still null: no poll has
    # run. Conflating the two is how a screen ends up claiming a window nothing searched.
    assert listed[source_id]["first_sync_days"] is None


def test_a_window_nobody_chose_is_reported_as_unset_rather_than_guessed_at(
    api: TestClient,
) -> None:
    """The fallback is a variable only the worker is given, so the API does not invent it."""
    source_id = connect_gmail(api)
    listed = {row["id"]: row for row in api.get("/v1/sources", headers=AUTH).json()}
    assert listed[source_id]["configured_first_sync_days"] is None


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_an_impossible_window_is_refused(api: TestClient, days: int) -> None:
    """The ceiling is a real bound: a typo must not turn one connect into an archive crawl."""
    refused = api.post(
        "/v1/sources/connect",
        json={
            "provider": "gmail",
            "name": "Gmail",
            "redirect_uri": REDIRECT,
            "first_sync_days": days,
        },
        headers=AUTH,
    )
    assert refused.status_code == 422, refused.text


def test_a_resync_sets_the_window_asks_for_a_fresh_search_and_queues_a_poll(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """motet#139's repair, from the API's side.

    Two writes and an enqueue: the window the next search will use, the request that makes
    the next poll *begin* one, and the poll itself. The request goes in ``config`` because
    ``handle_poll`` rewrites the whole of ``sync_state`` and would put back a cursor this
    route had deleted — silently, whenever a sync happened to be in flight.
    """
    source_id = connect_gmail(api)
    _clear_poll_jobs(db)

    answered = api.post(
        f"/v1/sources/{source_id}/resync", json={"first_sync_days": 365}, headers=AUTH
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["configured_first_sync_days"] == 365

    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.config["first_sync_days"] == 365
    assert isinstance(source.config[RESYNC_REQUESTED_CONFIG_KEY], str)
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM jobs WHERE queue = %s AND state = 'ready'",
            (Queue.POLL.value,),
        )
        row = cur.fetchone()
    assert row is not None and row["n"] == 1, "something has to run the fresh search"


def test_a_resync_is_refused_for_anything_that_is_not_a_live_mailbox(
    api: TestClient,
) -> None:
    body = {"first_sync_days": 30}
    assert api.post("/v1/sources/src_nope/resync", json=body, headers=AUTH).status_code == 404
    paste = api.post(f"/v1/sources/{repo.PASTE_SOURCE_ID}/resync", json=body, headers=AUTH)
    assert paste.status_code == 409, "a paste source is not searched"

    source_id = connect_gmail(api)
    api.delete(f"/v1/sources/{source_id}/credentials", headers=AUTH)
    disconnected = api.post(f"/v1/sources/{source_id}/resync", json=body, headers=AUTH)
    assert disconnected.status_code == 409, "nothing to search with"

    refused = api.post(
        f"/v1/sources/{source_id}/resync", json={"first_sync_days": 3651}, headers=AUTH
    )
    assert refused.status_code == 422


def _clear_poll_jobs(db: psycopg.Connection[Any]) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE queue = %s", (Queue.POLL.value,))
    db.commit()


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
    # The exact values the fake provider issued — derived by hash from the code, so they
    # are specific to this exchange rather than a constant a passing test could be blind to.
    from motet_sources.fakes import _fake_token

    issued = {
        _fake_token("refresh", "fake-auth-code"),
        _fake_token("access", "fake-auth-code"),
    }
    assert all(issued), "the fake must actually have issued something to look for"

    for body in (
        json.dumps(done.json()),
        json.dumps(api.get("/v1/sources", headers=AUTH).json()),
    ):
        for secret in issued:
            assert secret not in body
        # And no field *shaped* like a credential, so a future response model that added
        # one is caught even though its value would be unknown to this test.
        for key in json.loads(body) if body.startswith("[") else [json.loads(body)]:
            assert not any(
                "token" in name or "secret" in name or "credential" in name for name in (key or {})
            ), f"a credential-shaped field reached a response: {sorted(key)}"


def test_a_vault_that_cannot_seal_answers_503_rather_than_500(api: TestClient) -> None:
    """The Gmail-connect bug, end to end, at the layer that decides what a browser is told.

    Sealing goes through Cloud KMS in a deployed environment, and everything KMS can
    refuse with — no permission, no key, no SDK in the image — used to escape as itself.
    The route catches `VaultError`, so a vendor exception went straight past it into an
    unhandled 500: the one response Starlette sends *outside* the CORS middleware, which
    a browser will not hand to the caller at all. `fetch` rejected with `TypeError:
    Failed to fetch`, naming no status and no cause, and that was the whole of what the
    user could report.

    The real `CloudKmsKeyManager` is used, with only its client stubbed, so the
    translation being tested is the one that runs in production. 503 rather than 500
    because nothing is wrong with the request: the capability is not available. The token
    is discarded rather than stored unsealed — invariant 8 has no degraded mode.
    """
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    assert started.status_code == 201, started.text

    class RefusingKms:
        def encrypt(self, request: dict[str, Any]) -> Any:
            raise PermissionError("caller does not have cloudkms...useToEncrypt")

    key = CloudKmsKeyManager("projects/secret-proj/locations/y/keyRings/z/cryptoKeys/k")
    key._client = RefusingKms()  # noqa: SLF001 — the SDK seam, and there is no other way in
    app.dependency_overrides[dek_wrapper] = lambda: key
    try:
        done = api.post(
            "/v1/sources/callback",
            json={"state": started.json()["state"], "code": "fake-auth-code"},
            headers=AUTH,
        )
    finally:
        app.dependency_overrides.pop(dek_wrapper, None)

    assert done.status_code == 503, done.text
    # And the reason is not the exception: a KMS refusal quotes the key resource path,
    # which is infrastructure topology.
    assert "useToEncrypt" not in done.text
    assert "secret-proj" not in done.text


def test_a_vault_that_will_not_build_answers_503_before_the_exchange(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half, and it is refused a dependency earlier.

    `dek_wrapper` is a *dependency*, so it resolves before the route body — which means
    the `except VaultError` around the seal cannot see a vault that failed to build at
    all. Translating it there rather than here is what keeps an unconfigured vault from
    being a 500 as well.
    """
    monkeypatch.setenv(BACKEND_ENV, "kms")
    monkeypatch.delenv(KMS_KEY_ENV, raising=False)
    refused = api.post(
        "/v1/sources/callback", json={"state": "st_anything", "code": "c"}, headers=AUTH
    )
    assert refused.status_code == 503, refused.text
    assert KMS_KEY_ENV in refused.text, "the message has to name the variable to set"

    # And that message is for the owner, not the internet. It is only ever reached behind
    # authentication because `user_id` precedes `wrapper` in the route signature and
    # FastAPI resolves dependencies in declaration order — which is worth an assertion,
    # since reordering two parameters is not a change anybody would read as security.
    anonymous = api.post("/v1/sources/callback", json={"state": "st_anything", "code": "c"})
    assert anonymous.status_code == 401, anonymous.text
    assert KMS_KEY_ENV not in anonymous.text


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


def test_a_source_reports_its_filter_its_window_and_what_its_last_poll_found(
    api: TestClient, _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three facts a poll used to leave only in a log line (motet#94, motet#95).

    Before the first poll there is a filter and nothing else; after it, the window that
    first sync was bounded to and what it found. The fake mailbox holds four messages, so
    the poll sees four, queues four and is caught up.
    """
    monkeypatch.setenv("MOTET_GMAIL_FIRST_SYNC_DAYS", "14")
    started = api.post(
        "/v1/sources/connect",
        json={
            "provider": "gmail",
            "name": "Gmail",
            "query": "label:newsletters",
            "redirect_uri": REDIRECT,
        },
        headers=AUTH,
    )
    done = api.post(
        "/v1/sources/callback",
        json={"state": started.json()["state"], "code": "fake-auth-code"},
        headers=AUTH,
    )
    assert done.status_code == 200, done.text
    assert done.json()["query"] == "label:newsletters"
    assert (done.json()["first_sync_days"], done.json()["last_sync"]) == (None, None)

    drain(Queue.POLL, _migrated)

    listed = api.get("/v1/sources", headers=AUTH).json()
    gmail = next(source for source in listed if source["id"] == started.json()["source_id"])
    assert gmail["query"] == "label:newsletters"
    assert gmail["first_sync_days"] == 14
    last = gmail["last_sync"]
    assert (last["seen"], last["queued"], last["caught_up"], last["error"]) == (4, 4, True, None)
    assert last["at"]

    paste = next(source for source in listed if source["id"] == repo.PASTE_SOURCE_ID)
    assert (paste["query"], paste["first_sync_days"], paste["last_sync"]) == (None, None, None)


def test_a_source_with_no_filter_of_its_own_reports_the_default_it_is_polled_with(
    api: TestClient,
) -> None:
    source_id = connect_gmail(api)
    listed = api.get("/v1/sources", headers=AUTH).json()
    gmail = next(source for source in listed if source["id"] == source_id)
    assert gmail["query"] == DEFAULT_QUERY


def test_an_unreadable_last_sync_is_reported_as_absent_not_as_a_500(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id = connect_gmail(api)
    phase2.set_source_sync_state(db, source_id, {"last_sync": {"at": "not a time"}})
    db.commit()
    listed = api.get("/v1/sources", headers=AUTH)
    assert listed.status_code == 200
    gmail = next(source for source in listed.json() if source["id"] == source_id)
    assert gmail["last_sync"] is None


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


def test_disconnecting_records_when_so_it_is_not_an_abandoned_consent(
    api: TestClient,
) -> None:
    """motet#90 gap 6: without it the two rows were identical but for `last_polled_at`."""
    source_id = connect_gmail(api)
    abandoned = start_consent(api)
    assert api.delete(f"/v1/sources/{source_id}/credentials", headers=AUTH).status_code == 204
    # Disconnecting a row that never held a credential forgets nothing, so it records
    # nothing — otherwise it would become a "disconnected mailbox" nobody could dismiss.
    assert api.delete(f"/v1/sources/{abandoned}/credentials", headers=AUTH).status_code == 204

    listed = {source["id"]: source for source in api.get("/v1/sources", headers=AUTH).json()}
    assert listed[source_id]["connected"] is False
    assert listed[source_id]["disconnected_at"] is not None
    assert listed[abandoned]["disconnected_at"] is None
    assert api.delete(f"/v1/sources/{abandoned}", headers=AUTH).status_code == 204


def test_a_source_reports_what_it_has_pulled_in_all_time(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """motet#90 gap 5: per source, on every route that returns one, not per kind."""
    source_id = connect_gmail(api)
    other_id = connect_gmail(api)
    items = [
        phase2.insert_polled_source_item(
            db,
            user_id=repo.OWNER_USER_ID,
            source_id_=source_id,
            external_id=f"m{n}",
            title="T",
            text="B",
        )
        for n in range(3)
    ]
    db.execute("UPDATE source_items SET state = 'integrated' WHERE id = %s", (items[0],))
    db.commit()

    listed = {source["id"]: source for source in api.get("/v1/sources", headers=AUTH).json()}
    assert (listed[source_id]["items_pulled_in"], listed[source_id]["items_integrated"]) == (3, 1)
    assert (listed[other_id]["items_pulled_in"], listed[other_id]["items_integrated"]) == (0, 0)
    assert listed[repo.PASTE_SOURCE_ID]["items_pulled_in"] == 0

    polled = api.post(f"/v1/sources/{source_id}/poll", headers=AUTH).json()
    assert (polled["items_pulled_in"], polled["items_integrated"]) == (3, 1)


class TestRemovingASource:
    """motet#90 gap 7: `DELETE /v1/sources/{id}` dismisses an abandoned consent, only.

    The delete cascades to source items and to the claims and highlights citing them, so
    every refusal here is data somebody has. Each guard is asserted through the route.
    """

    def test_an_abandoned_consent_is_removed(self, api: TestClient) -> None:
        source_id = start_consent(api)
        assert api.delete(f"/v1/sources/{source_id}", headers=AUTH).status_code == 204
        ids = {source["id"] for source in api.get("/v1/sources", headers=AUTH).json()}
        assert source_id not in ids

    def test_another_users_source_is_a_404_and_survives(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        db.execute("INSERT INTO users (id) VALUES ('remove-route-other')")
        try:
            theirs = phase2.create_source(
                db, user_id="remove-route-other", kind="gmail", name="Theirs"
            )
            phase2.set_source_active(db, theirs.id, active=False)
            db.commit()

            refused = api.delete(f"/v1/sources/{theirs.id}", headers=AUTH)
            assert refused.status_code == 404
            assert phase2.get_source(db, theirs.id) is not None
        finally:
            db.execute("DELETE FROM users WHERE id = 'remove-route-other'")
            db.commit()

    def test_a_connected_source_is_refused(self, api: TestClient) -> None:
        source_id = connect_gmail(api)
        refused = api.delete(f"/v1/sources/{source_id}", headers=AUTH)
        assert refused.status_code == 409
        assert "Disconnect it instead" in refused.json()["detail"]
        assert source_id in {source["id"] for source in api.get("/v1/sources", headers=AUTH).json()}

    def test_a_disconnected_source_is_still_refused(self, api: TestClient) -> None:
        """No credential *now* is not "never connected": what it pulled in stays cited."""
        source_id = connect_gmail(api)
        api.delete(f"/v1/sources/{source_id}/credentials", headers=AUTH)
        assert api.delete(f"/v1/sources/{source_id}", headers=AUTH).status_code == 409

    def test_a_source_with_items_is_refused(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        source_id = start_consent(api)
        phase2.insert_polled_source_item(
            db,
            user_id=repo.OWNER_USER_ID,
            source_id_=source_id,
            external_id="m",
            title="T",
            text="B",
        )
        db.commit()
        refused = api.delete(f"/v1/sources/{source_id}", headers=AUTH)
        assert refused.status_code == 409
        assert "delete them" in refused.json()["detail"]
        assert phase2.get_source(db, source_id) is not None

    def test_the_paste_source_is_refused(self, api: TestClient) -> None:
        refused = api.delete(f"/v1/sources/{repo.PASTE_SOURCE_ID}", headers=AUTH)
        assert refused.status_code == 409
        assert "built in" in refused.json()["detail"]

    def test_an_unknown_source_is_a_404(self, api: TestClient) -> None:
        assert api.delete("/v1/sources/src_nope", headers=AUTH).status_code == 404

    def test_a_callback_after_removal_finds_nothing(self, api: TestClient) -> None:
        """The removed row's authorization goes with it, so a late return cannot land."""
        started = api.post(
            "/v1/sources/connect",
            json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
            headers=AUTH,
        ).json()
        api.delete(f"/v1/sources/{started['source_id']}", headers=AUTH)
        late = api.post(
            "/v1/sources/callback", json={"state": started["state"], "code": "c"}, headers=AUTH
        )
        assert late.status_code == 400


def start_consent(api: TestClient) -> str:
    """Begin connecting a mailbox and never come back — a cancelled consent's row."""
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    assert started.status_code == 201, started.text
    return str(started.json()["source_id"])


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
        ("put", "/v1/episodes/ep_x/position", {"listened_through_ms": 0}),
        ("delete", "/v1/sources/src_x", None),
        ("delete", "/v1/sources/src_x/credentials", None),
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


# --- episodes from a pick ------------------------------------------------------------


def paste_backlog(api: TestClient, url: str) -> list[dict[str, Any]]:
    for title, text in NEWSLETTERS:
        api.post("/v1/sources/paste", json={"title": title, "text": text}, headers=AUTH)
    drain(Queue.INTEGRATE, url)
    backlog: list[dict[str, Any]] = api.get("/v1/news-items", headers=AUTH).json()
    assert len(backlog) == len(NEWSLETTERS)
    return backlog


def create_picked(api: TestClient, ids: list[str], **extra: Any) -> Any:
    return api.post(
        "/v1/episodes",
        json={"title": "Picked", "max_duration_ms": 1_200_000, "news_item_ids": ids, **extra},
        headers=AUTH,
    )


def test_a_picked_episode_is_made_of_only_what_was_picked(api: TestClient, _migrated: str) -> None:
    """The rest of the backlog is unread and would be chosen by a manual episode."""
    backlog = paste_backlog(api, _migrated)
    picked = backlog[-1]["id"]

    created = create_picked(api, [picked])
    assert created.status_code == 201, created.text
    assert created.json()["keep_in_backlog"] is False, "consuming is still the default"
    run_pipeline(_migrated)

    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "ready", episode.get("last_error")
    assert [segment["news_item_id"] for segment in episode["segments"]] == [picked]


def test_a_pick_may_include_a_story_already_read(api: TestClient, _migrated: str) -> None:
    """A pick is a decision about these stories, so read state does not filter it."""
    backlog = paste_backlog(api, _migrated)
    ids = [item["id"] for item in backlog]
    api.patch(f"/v1/news-items/{ids[0]}", json={"read": True}, headers=AUTH)

    created = create_picked(api, ids)
    assert created.status_code == 201, created.text
    drain(Queue.ASSEMBLE, _migrated)

    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert sorted(segment["news_item_id"] for segment in episode["segments"]) == sorted(ids)


def test_keep_in_backlog_leaves_every_story_unread_however_far_you_listen(
    api: TestClient, _migrated: str
) -> None:
    backlog = paste_backlog(api, _migrated)
    ids = [item["id"] for item in backlog]

    created = create_picked(api, ids, keep_in_backlog=True)
    assert created.status_code == 201, created.text
    assert created.json()["keep_in_backlog"] is True
    run_pipeline(_migrated)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "ready", episode.get("last_error")

    moved = api.put(
        f"/v1/episodes/{episode['id']}/position",
        json={"listened_through_ms": episode["duration_ms"]},
        headers=AUTH,
    ).json()
    assert moved["listened_through_ms"] == episode["duration_ms"], "the position still moves"
    assert moved["news_items_marked_read"] == 0

    listened = api.post(f"/v1/episodes/{episode['id']}/listened", headers=AUTH).json()
    assert listened["news_items_marked_read"] == 0

    after = api.get("/v1/news-items", headers=AUTH).json()
    assert not any(item["read"] for item in after), "every picked story is still in the backlog"


def test_an_ordinary_episode_still_marks_what_you_listened_past(
    api: TestClient, _migrated: str
) -> None:
    """The flag is per episode: without it a pick consumes exactly like 'all unread'."""
    backlog = paste_backlog(api, _migrated)
    created = create_picked(api, [item["id"] for item in backlog])
    run_pipeline(_migrated)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()

    listened = api.post(f"/v1/episodes/{episode['id']}/listened", headers=AUTH).json()
    assert listened["news_items_marked_read"] == len(backlog)


def test_a_pick_naming_an_unknown_story_is_refused_and_creates_nothing(
    api: TestClient, _migrated: str
) -> None:
    backlog = paste_backlog(api, _migrated)
    refused = create_picked(api, [backlog[0]["id"], "ni_does_not_exist"])
    assert refused.status_code == 422
    assert "1 of the picked news items" in refused.text
    assert api.get("/v1/episodes", headers=AUTH).json() == []


def test_a_pick_naming_another_users_story_is_refused(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str
) -> None:
    """Refused the same way as an id that does not exist: no oracle for whose it is."""
    backlog = paste_backlog(api, _migrated)
    db.execute("INSERT INTO users (id) VALUES ('pick-other') ON CONFLICT DO NOTHING")
    db.execute(
        "INSERT INTO news_items (id, user_id, title, summary) "
        "VALUES ('ni_theirs', 'pick-other', 'Theirs', 'Not yours.')"
    )
    db.commit()

    refused = create_picked(api, [backlog[0]["id"], "ni_theirs"])
    assert refused.status_code == 422
    assert "Theirs" not in refused.text
    assert api.get("/v1/episodes", headers=AUTH).json() == []


def test_a_pick_whose_stories_vanished_fails_saying_so(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str
) -> None:
    backlog = paste_backlog(api, _migrated)
    created = create_picked(api, [backlog[0]["id"]])
    db.execute("DELETE FROM news_item_sources WHERE news_item_id = %s", (backlog[0]["id"],))
    db.execute("DELETE FROM news_items WHERE id = %s", (backlog[0]["id"],))
    db.commit()
    drain(Queue.ASSEMBLE, _migrated)
    episode = api.get(f"/v1/episodes/{created.json()['id']}", headers=AUTH).json()
    assert episode["state"] == "failed"
    assert "none of the 1 picked news items exist" in (episode["last_error"] or "")


def test_the_smart_route_cannot_carry_a_pick(api: TestClient, _migrated: str) -> None:
    """Ownership is checked on `/v1/episodes`; the smart rule's model has no such field.

    Pydantic ignores the unknown key, so the episode is an ordinary rule — never a pick of
    ids nobody checked.
    """
    paste_backlog(api, _migrated)
    created = api.post(
        "/v1/episodes/smart",
        json={"title": "S", "max_duration_ms": 600_000, "rule": {"news_item_ids": ["ni_x"]}},
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    with psycopg.connect(_migrated) as conn:
        row = conn.execute(
            "SELECT rule FROM episodes WHERE id = %s", (created.json()["id"],)
        ).fetchone()
    assert row is not None and row[0]["news_item_ids"] == []


def test_an_empty_or_oversized_pick_is_refused_by_the_contract(api: TestClient) -> None:
    assert create_picked(api, []).status_code == 422
    assert create_picked(api, [f"ni_{n}" for n in range(101)]).status_code == 422


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


# --- the position resource (motet#11) -------------------------------------------------


def test_the_position_is_served_back_on_the_episode(api: TestClient, _migrated: str) -> None:
    """motet#11: a device that never played the episode can still resume.

    The write half already existed as ``POST .../progress``; nothing read it back, so the
    position lived on whichever device did the listening. This is the read half.
    """
    episode = paste_and_render(api, _migrated)
    assert episode["listened_through_ms"] == 0, "a fresh episode starts at the beginning"

    stored = api.put(
        f"/v1/episodes/{episode['id']}/position",
        json={"listened_through_ms": 12_000},
        headers=AUTH,
    )
    assert stored.status_code == 200
    assert stored.json()["listened_through_ms"] == 12_000

    fetched = api.get(f"/v1/episodes/{episode['id']}", headers=AUTH).json()
    assert fetched["listened_through_ms"] == 12_000, "a second device can resume from here"

    listed = api.get("/v1/episodes", headers=AUTH).json()
    assert [item["listened_through_ms"] for item in listed if item["id"] == episode["id"]] == [
        12_000
    ], "the list a client loads on mount carries it too"


def test_the_position_resource_is_the_same_write_as_progress(
    api: TestClient, _migrated: str
) -> None:
    """One column, two spellings — so the two routes can never disagree."""
    episode = paste_and_render(api, _migrated)
    first_end = episode["segments"][0]["start_ms"] + episode["segments"][0]["duration_ms"]

    put = api.put(
        f"/v1/episodes/{episode['id']}/position",
        json={"listened_through_ms": first_end},
        headers=AUTH,
    ).json()
    assert put["episode_id"] == episode["id"]
    assert put["news_items_marked_read"] == 1, "invariant 5 still runs off this write"

    posted = api.post(
        f"/v1/episodes/{episode['id']}/progress",
        json={"listened_through_ms": first_end},
        headers=AUTH,
    ).json()
    assert posted["listened_through_ms"] == put["listened_through_ms"]
    assert posted["news_items_marked_read"] == 0, "idempotent: nothing left to mark"


def test_the_position_does_not_go_backwards(api: TestClient, _migrated: str) -> None:
    """Invariant 4: we own the position, so a stale outbox write cannot rewind a walk."""
    episode = paste_and_render(api, _migrated)
    api.put(
        f"/v1/episodes/{episode['id']}/position",
        json={"listened_through_ms": episode["duration_ms"]},
        headers=AUTH,
    )
    rewound = api.put(
        f"/v1/episodes/{episode['id']}/position",
        json={"listened_through_ms": 0},
        headers=AUTH,
    ).json()
    assert rewound["listened_through_ms"] == episode["duration_ms"]

    fetched = api.get(f"/v1/episodes/{episode['id']}", headers=AUTH).json()
    assert fetched["listened_through_ms"] == episode["duration_ms"]


def test_setting_a_position_on_an_unknown_episode_is_a_404(api: TestClient) -> None:
    assert (
        api.put(
            "/v1/episodes/ep_nope/position",
            json={"listened_through_ms": 1},
            headers=AUTH,
        ).status_code
        == 404
    )


def test_a_negative_position_is_refused_by_the_position_route_too(api: TestClient) -> None:
    assert (
        api.put(
            "/v1/episodes/ep_x/position",
            json={"listened_through_ms": -1},
            headers=AUTH,
        ).status_code
        == 422
    )


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


def test_a_highlight_naming_a_story_that_does_not_exist_is_a_422_not_a_500(
    api: TestClient, _migrated: str
) -> None:
    """A hallucinated `news_item_id` must be an ordinary bad argument.

    This is what the voice tool call actually gets wrong. If the foreign key were the only
    check, the same request would surface as an unhandled 500 — and the span here is
    valid, so nothing upstream would have caught it first.
    """
    episode = paste_and_render(api, _migrated)
    claim = episode["segments"][0]["claims"][0]
    refused = api.post(
        "/v1/highlights",
        json={
            "news_item_id": "ni_hallucinated",
            "source_item_id": claim["span"]["source_item_id"],
            "span_start": claim["span"]["start"],
            "span_end": claim["span"]["end"],
        },
        headers=AUTH,
    )
    assert refused.status_code == 422, refused.text
    assert api.get("/v1/highlights", headers=AUTH).json() == []


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
