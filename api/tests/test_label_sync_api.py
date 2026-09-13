"""Label sync's API half — the settings, and the scope it is allowed to ask for (motet#96).

The property under test is the one the issue gate asked a reviewer to check: **the wider
``gmail.modify`` grant is opt-in per source.** A mailbox connected the ordinary way is asked
for ``gmail.readonly`` and nothing else, setting labels widens nothing on its own, and the
one route that asks for more refuses until the source has labels set. Checked on the
authorization URL itself and on the stored ``oauth_states`` row, because those are what
Google is actually shown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_db import CredentialPurpose, phase2, repo
from motet_sources import (
    FAKE_LABELS,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    LABEL_SYNC_SCOPES,
    FakeOAuthClient,
)
from motet_sources.labels import CATALOG_KEY, CONFIG_KEY, catalog_to_sync_state

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REDIRECT = "https://app.example.invalid/oauth/callback"
OWNER_PAIR = {"remove_label": "Newsletters", "add_label": "Completed"}


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


def connect(api: TestClient) -> tuple[str, str]:
    """The ordinary connect, through the fake provider. Returns (source id, consent URL)."""
    started = api.post(
        "/v1/sources/connect",
        json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
        headers=AUTH,
    )
    assert started.status_code == 201, started.text
    body = started.json()
    done = api.post(
        "/v1/sources/callback", json={"state": body["state"], "code": "code-1"}, headers=AUTH
    )
    assert done.status_code == 200, done.text
    return str(body["source_id"]), str(body["authorization_url"])


def scopes_in(url: str) -> list[str]:
    return parse_qs(urlparse(url).query)["scope"][0].split()


def source_json(api: TestClient, source_id: str) -> dict[str, Any]:
    listed = api.get("/v1/sources", headers=AUTH).json()
    return dict(next(source for source in listed if source["id"] == source_id))


def pending_scopes(db: psycopg.Connection[Any]) -> list[str]:
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT scopes FROM oauth_states ORDER BY created_at")
        return [row["scopes"] for row in cur.fetchall()]


# --- (c) a read-only source is unchanged -------------------------------------------------


def test_connecting_a_mailbox_still_asks_for_readonly_and_nothing_else(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """The scope every connect has always asked for, exactly — not merely including it."""
    source_id, url = connect(api)
    assert scopes_in(url) == [GMAIL_READONLY_SCOPE]
    assert GMAIL_MODIFY_SCOPE not in url

    stored = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert stored is not None and stored.scopes == (GMAIL_READONLY_SCOPE,)
    label_sync = source_json(api, source_id)["label_sync"]
    assert label_sync["status"] == "off"
    assert label_sync["modify_granted"] is False


def test_setting_labels_on_a_read_only_mailbox_widens_nothing(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """Choosing labels is a setting, not a consent. The grant is what it was."""
    source_id, _ = connect(api)
    states_before = pending_scopes(db)

    saved = api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    assert saved.status_code == 200, saved.text
    label_sync = saved.json()["label_sync"]
    assert label_sync["status"] == "needs_reauthorization"
    assert label_sync["remove_label"] == "Newsletters"
    assert label_sync["add_label"] == "Completed"
    assert saved.json()["scopes"] == [GMAIL_READONLY_SCOPE]
    assert pending_scopes(db) == states_before, "no consent was started"


def test_the_wider_scope_cannot_be_asked_for_until_labels_are_set(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id, _ = connect(api)
    states_before = pending_scopes(db)
    refused = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    )
    assert refused.status_code == 409
    assert "label" in refused.json()["detail"]
    assert pending_scopes(db) == states_before


def test_a_grant_wider_than_the_connect_asked_for_is_recorded_as_asked(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that folds another source's label-sync grant into a new token changes
    nothing: the worker acts on the recorded scopes, and those are what this consent asked
    for."""
    monkeypatch.setattr(
        "motet_api.main.build_oauth_client",
        lambda env=None: FakeOAuthClient(granted_scopes=LABEL_SYNC_SCOPES),
    )
    source_id, url = connect(api)
    assert scopes_in(url) == [GMAIL_READONLY_SCOPE]
    stored = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert stored is not None and stored.scopes == (GMAIL_READONLY_SCOPE,)
    label_sync = source_json(api, source_id)["label_sync"]
    assert label_sync["modify_granted"] is False


def test_every_consent_is_left_for_a_worker_to_check_the_mailbox_of(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """A source checked for one grant is due a check again the moment a consent replaces it."""
    from motet_workers.ingest import mailbox_check_due

    source_id, _ = connect(api)
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None
    stamp = mailbox_check_due(db, source)
    assert stamp is not None, "a new grant nobody has checked"
    phase2.merge_source_sync_state(db, source_id, "mailbox_address", "owner@example.invalid")
    phase2.merge_source_sync_state(db, source_id, "mailbox_verified_for", stamp)
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None and mailbox_check_due(db, source) is None, "checked for it"

    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    ).json()
    done = api.post(
        "/v1/sources/callback", json={"state": started["state"], "code": "c2"}, headers=AUTH
    )
    assert done.status_code == 200, done.text
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None
    assert mailbox_check_due(db, source) not in (None, stamp), "re-consented: a new grant"


# --- the re-consent ------------------------------------------------------------------


def test_reauthorizing_asks_for_modify_for_that_source_only(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """The wide consent is bound to the one source that chose it; the next connect is not."""
    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)

    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["source_id"] == source_id
    assert scopes_in(body["authorization_url"]) == list(LABEL_SYNC_SCOPES)
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT source_id, scopes FROM oauth_states WHERE state = %s", (body["state"],))
        row = cur.fetchone()
    assert row is not None and row["source_id"] == source_id
    assert row["scopes"].split() == list(LABEL_SYNC_SCOPES)

    # No address recorded yet, so no hint — and the worker will record the one it sees.
    assert "login_hint" not in body["authorization_url"]

    # Another mailbox, connected afterwards, is asked for exactly what every connect is.
    _, second_url = connect(api)
    assert scopes_in(second_url) == [GMAIL_READONLY_SCOPE]


def test_completing_the_reconsent_turns_label_sync_on(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's click, played by the fake provider granting what was asked."""
    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    ).json()

    monkeypatch.setattr(
        "motet_api.main.build_oauth_client",
        lambda env=None: FakeOAuthClient(granted_scopes=LABEL_SYNC_SCOPES),
    )
    done = api.post(
        "/v1/sources/callback", json={"state": started["state"], "code": "code-2"}, headers=AUTH
    )
    assert done.status_code == 200, done.text
    assert done.json()["label_sync"]["status"] == "on"
    assert done.json()["label_sync"]["modify_granted"] is True
    stored = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert stored is not None and GMAIL_MODIFY_SCOPE in stored.scopes
    assert source_json(api, source_id)["label_sync"]["status"] == "on"


def test_reauthorizing_hints_the_mailbox_the_source_already_reads(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id, _ = connect(api)
    phase2.merge_source_sync_state(db, source_id, "mailbox_address", "owner@example.invalid")
    db.commit()
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    ).json()
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["login_hint"] == ["owner@example.invalid"]


def test_failures_from_before_the_reconsent_stop_counting_after_it(
    api: TestClient, db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Re-authorize" must not outlive the re-authorization it asked for."""
    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    item = phase2.insert_polled_source_item(
        db, user_id=repo.OWNER_USER_ID, source_id_=source_id, external_id="m1", title="A", text="a"
    )
    assert item
    phase2.record_label_writeback(db, item, error="This mailbox was connected read-only.")
    db.commit()
    assert source_json(api, source_id)["label_sync"]["failed_items"] == 1

    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    ).json()
    monkeypatch.setattr(
        "motet_api.main.build_oauth_client",
        lambda env=None: FakeOAuthClient(granted_scopes=LABEL_SYNC_SCOPES),
    )
    api.post(
        "/v1/sources/callback", json={"state": started["state"], "code": "code-3"}, headers=AUTH
    )
    label_sync = source_json(api, source_id)["label_sync"]
    assert label_sync["status"] == "on"
    assert label_sync["failed_items"] == 0
    assert label_sync["last_error"] is None


def test_ingest_now_through_the_api_moves_the_message_end_to_end(
    api: TestClient, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole feature as the owner uses it, over HTTP, with the real worker behind it.

    Connect read-only, choose labels, re-consent, sync, then press "Ingest now" on one held
    item (#91's route). Exactly one modify, on exactly that item's message, and nothing for
    the items left held — which is motet#96's trigger proved at the route rather than at a
    helper.
    """
    from motet_sources import FakeMailClient
    from motet_workers import Queue, drain

    mailbox = FakeMailClient()
    monkeypatch.setattr("motet_workers.ingest.build_mail_client", lambda token, env=None: mailbox)
    monkeypatch.setattr("motet_workers.labels.build_mail_client", lambda token, env=None: mailbox)
    wide = FakeOAuthClient(granted_scopes=LABEL_SYNC_SCOPES)
    monkeypatch.setattr("motet_workers.ingest.build_oauth_client", lambda env=None: wide)

    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    started = api.post(
        f"/v1/sources/{source_id}/reauthorize", json={"redirect_uri": REDIRECT}, headers=AUTH
    ).json()
    monkeypatch.setattr("motet_api.main.build_oauth_client", lambda env=None: wide)
    assert (
        api.post(
            "/v1/sources/callback", json={"state": started["state"], "code": "c"}, headers=AUTH
        ).status_code
        == 200
    )

    drain(Queue.POLL, _migrated)
    drain(Queue.EXTRACT, _migrated)
    held = api.get("/v1/source-items/held", headers=AUTH).json()
    assert len(held) >= 2 and mailbox.modify_calls == [], "polling and holding wrote nothing"

    chosen = held[0]["id"]
    queued = api.post("/v1/source-items/integrate", json={"ids": [chosen]}, headers=AUTH)
    assert queued.json() == {"queued": 1, "skipped": 0}
    assert drain(Queue.INTEGRATE, _migrated) == 1

    db.commit()
    external = db.execute(
        "SELECT external_id FROM source_items WHERE id = %s", (chosen,)
    ).fetchone()
    assert external is not None
    assert mailbox.modify_calls == [(external["external_id"], ("Label_102",), ("Label_101",))]
    label_sync = source_json(api, source_id)["label_sync"]
    assert label_sync["status"] == "on"
    assert label_sync["last_synced_at"] is not None
    assert label_sync["failed_items"] == 0


# --- the settings --------------------------------------------------------------------


def test_clearing_both_labels_turns_label_sync_off(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    cleared = api.put(
        f"/v1/sources/{source_id}/label-sync",
        json={"remove_label": "", "add_label": None},
        headers=AUTH,
    )
    assert cleared.status_code == 200
    assert cleared.json()["label_sync"]["status"] == "off"
    db.commit()
    source = phase2.get_source(db, source_id)
    assert source is not None and CONFIG_KEY not in source.config


def test_a_setting_that_changes_one_label_keeps_the_rest_of_the_config(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """``config`` also holds the Gmail query; a label change must not clobber it."""
    started = api.post(
        "/v1/sources/connect",
        json={
            "provider": "gmail",
            "name": "Gmail",
            "query": "label:news",
            "redirect_uri": REDIRECT,
        },
        headers=AUTH,
    ).json()
    api.put(
        f"/v1/sources/{started['source_id']}/label-sync",
        json={"add_label": "Completed"},
        headers=AUTH,
    )
    db.commit()
    source = phase2.get_source(db, started["source_id"])
    assert source is not None
    assert source.config["query"] == "label:news"
    assert source.config[CONFIG_KEY] == {"remove": None, "add": "Completed"}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"add_label": "TRASH"}, "system label"),
        ({"remove_label": "INBOX", "add_label": "SPAM"}, "system label"),
        ({"remove_label": "Completed", "add_label": "completed"}, "same"),
    ],
)
def test_a_label_setting_that_could_lose_mail_is_refused(
    api: TestClient, body: dict[str, str], fragment: str
) -> None:
    source_id, _ = connect(api)
    refused = api.put(f"/v1/sources/{source_id}/label-sync", json=body, headers=AUTH)
    assert refused.status_code == 422
    assert fragment in refused.json()["detail"]


def test_only_a_mailbox_has_labels(api: TestClient) -> None:
    for path in ("label-sync", "reauthorize"):
        method = api.put if path == "label-sync" else api.post
        body = OWNER_PAIR if path == "label-sync" else {"redirect_uri": REDIRECT}
        paste = method(f"/v1/sources/{repo.PASTE_SOURCE_ID}/{path}", json=body, headers=AUTH)
        assert paste.status_code == 409
        missing = method(f"/v1/sources/src_nope/{path}", json=body, headers=AUTH)
        assert missing.status_code == 404
    listed = api.get("/v1/sources", headers=AUTH).json()
    paste_row = next(source for source in listed if source["id"] == repo.PASTE_SOURCE_ID)
    assert paste_row["label_sync"] is None


def test_the_source_reports_the_pickers_and_what_the_write_back_did(
    api: TestClient, db: psycopg.Connection[Any]
) -> None:
    """Everything the Sources screen shows, read with no key and no Gmail call."""
    source_id, _ = connect(api)
    api.put(f"/v1/sources/{source_id}/label-sync", json=OWNER_PAIR, headers=AUTH)
    at = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)
    phase2.merge_source_sync_state(
        db, source_id, CATALOG_KEY, catalog_to_sync_state(FAKE_LABELS, fetched_at=at)
    )
    ok = phase2.insert_polled_source_item(
        db, user_id=repo.OWNER_USER_ID, source_id_=source_id, external_id="m1", title="A", text="a"
    )
    bad = phase2.insert_polled_source_item(
        db, user_id=repo.OWNER_USER_ID, source_id_=source_id, external_id="m2", title="B", text="b"
    )
    assert ok and bad
    phase2.record_label_writeback(db, ok, error=None)
    phase2.record_label_writeback(db, bad, error="Gmail is unavailable (503)")
    db.commit()

    label_sync = source_json(api, source_id)["label_sync"]
    assert label_sync["available_labels"] == [
        "Completed",
        "Newsletters",
        "Reading/Later",
        "IMPORTANT",
        "INBOX",
        "STARRED",
        "UNREAD",
    ]
    assert label_sync["labels_read_at"].startswith("2026-09-13T04:00")
    assert label_sync["last_synced_at"] is not None
    assert label_sync["failed_items"] == 1
    assert label_sync["last_error"] == "Gmail is unavailable (503)"


def test_the_label_sync_routes_require_authentication(api: TestClient) -> None:
    assert api.put("/v1/sources/src_x/label-sync", json=OWNER_PAIR).status_code == 401
    assert (
        api.post("/v1/sources/src_x/reauthorize", json={"redirect_uri": REDIRECT}).status_code
        == 401
    )
