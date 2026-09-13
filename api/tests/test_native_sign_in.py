"""The iOS app's sign-in: the web sign-in, and a one-time code that carries it back.

Chosen by Tadas on 2026-09-13 (AGENTS.md, "The phone signs in through the web sign-in").
The round trip here is the one the app and its in-app browser make, against the fake
identity provider and a real Postgres:

1. the app starts it with a PKCE challenge (``/v1/auth/native/start``);
2. the web app posts Google's code and state to ``/v1/auth/google/callback``, as a browser
   does, and is answered with a ``motet://`` handoff link instead of a session;
3. the app redeems the link's code with its verifier (``/v1/auth/native/redeem``).

What these tests hold is the security shape of that: the browser that finished the
sign-in never holds a session, the link carries a code rather than a token, and the code is
single-use, short-lived, bound to the verifier, and re-checked against the allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import ALLOWED_EMAILS_ENV, FAKE_EMAIL
from motet_api.config import CALLBACK_PATH
from motet_api.deps import reset_store
from motet_db import auth as auth_repo

TOKEN = "test-api-token"
APP_ORIGIN = "https://app.example.invalid"


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "fake")
    monkeypatch.setenv("MOTET_APP_BASE_URL", APP_ORIGIN)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, FAKE_EMAIL)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge, made the way the app makes them."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start(api: TestClient, challenge: str, app_link_domain: str | None = None) -> dict[str, Any]:
    body_out: dict[str, Any] = {"code_challenge": challenge}
    if app_link_domain is not None:
        body_out["app_link_domain"] = app_link_domain
    response = api.post("/v1/auth/native/start", json=body_out)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def finish_in_the_web_app(api: TestClient, started: dict[str, Any]) -> dict[str, Any]:
    """What the SPA's /oauth/callback does with the code and state Google returned.

    The fake provider's authorization URL points straight back at the redirect URI with
    both, which is what Google does once a human has said yes.
    """
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    response = api.post(
        "/v1/auth/google/callback",
        json={"state": query["state"][0], "code": query["code"][0]},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def handoff_code(login: dict[str, Any]) -> str:
    link = urlsplit(login["handoff_url"])
    assert (link.scheme, link.netloc) == ("motet", "signed-in")
    return parse_qs(link.query)["code"][0]


def redeem(api: TestClient, code: str, verifier: str) -> Any:
    return api.post("/v1/auth/native/redeem", json={"code": code, "code_verifier": verifier})


def signed_in_phone(api: TestClient) -> dict[str, Any]:
    verifier, challenge = pkce()
    code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
    response = redeem(api, code, verifier)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def session_count(db: psycopg.Connection[Any]) -> int:
    row = db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()
    assert row is not None
    return int(row["n"])


class TestThePhoneSignsIn:
    def test_the_round_trip_ends_in_a_session_the_app_holds(self, api: TestClient) -> None:
        verifier, challenge = pkce()
        started = start(api, challenge)
        assert started["callback_scheme"] == "motet"

        login = finish_in_the_web_app(api, started)
        assert login["email"] == FAKE_EMAIL
        assert login["token"] is None
        assert login["expires_at"] is None

        redeemed = redeem(api, handoff_code(login), verifier)
        assert redeemed.status_code == 200, redeemed.text
        session = redeemed.json()
        assert session["email"] == FAKE_EMAIL
        assert session["expires_at"] is not None
        assert session["handoff_url"] is None

        headers = {"Authorization": f"Bearer {session['token']}"}
        assert api.get("/v1/news-items", headers=headers).status_code == 200
        assert api.get("/v1/auth/session", headers=headers).json()["how"] == "session"

    def test_google_returns_to_the_web_apps_registered_callback(self, api: TestClient) -> None:
        """The phone needs no Google client of its own because it borrows this redirect."""
        _, challenge = pkce()
        started = start(api, challenge)
        assert started["authorization_url"].startswith(f"{APP_ORIGIN}{CALLBACK_PATH}?")

    def test_the_browser_that_finished_it_is_never_given_a_session(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The in-app browser shares Safari's storage; a session there would outlive the flow."""
        _, challenge = pkce()
        finish_in_the_web_app(api, start(api, challenge))
        assert session_count(db) == 0

    def test_a_browser_sign_in_still_gets_its_token_directly(self, api: TestClient) -> None:
        started = api.post(
            "/v1/auth/google/start", json={"redirect_uri": f"{APP_ORIGIN}{CALLBACK_PATH}"}
        ).json()
        query = parse_qs(urlsplit(started["authorization_url"]).query)
        login = api.post(
            "/v1/auth/google/callback",
            json={"state": query["state"][0], "code": query["code"][0]},
        ).json()
        assert login["token"]
        assert login["handoff_url"] is None


class TestTheHandoffCode:
    def test_the_link_carries_a_code_and_nothing_else(self, api: TestClient) -> None:
        _, challenge = pkce()
        login = finish_in_the_web_app(api, start(api, challenge))
        assert set(parse_qs(urlsplit(login["handoff_url"]).query)) == {"code"}

    def test_a_code_is_good_for_one_redeem(self, api: TestClient) -> None:
        verifier, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        assert redeem(api, code, verifier).status_code == 200
        assert redeem(api, code, verifier).status_code == 400

    def test_a_refused_redeem_leaves_the_code_for_the_app_with_the_verifier(
        self, api: TestClient
    ) -> None:
        """The refusal rolls back with its request, so a guess cannot burn the real sign-in."""
        verifier, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        somebody_elses, _ = pkce()
        assert redeem(api, code, somebody_elses).status_code == 400
        assert redeem(api, code, verifier).status_code == 200

    def test_a_code_without_its_verifier_gets_nothing(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """What another app that registered motet:// would hold: the code, not the verifier."""
        _, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        somebody_elses, _ = pkce()
        assert redeem(api, code, somebody_elses).status_code == 400
        assert session_count(db) == 0

    def test_an_expired_code_is_refused(self, api: TestClient, db: psycopg.Connection[Any]) -> None:
        verifier, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        db.execute("UPDATE auth_handoffs SET expires_at = now() - interval '1 second'")
        db.commit()
        assert redeem(api, code, verifier).status_code == 400

    def test_the_code_is_stored_only_as_a_hash(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        _, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        rows = db.execute("SELECT code_sha256 FROM auth_handoffs").fetchall()
        assert [row["code_sha256"] for row in rows] == [auth_repo.token_digest(code)]

    def test_an_address_removed_before_the_redeem_gets_nothing(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection[Any]
    ) -> None:
        verifier, challenge = pkce()
        code = handoff_code(finish_in_the_web_app(api, start(api, challenge)))
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "somebody-else@example.invalid")
        assert redeem(api, code, verifier).status_code == 403
        assert session_count(db) == 0

    def test_the_session_it_mints_is_revocable_like_any_other(self, api: TestClient) -> None:
        session = signed_in_phone(api)
        headers = {"Authorization": f"Bearer {session['token']}"}
        assert api.post("/v1/auth/logout", headers=headers).status_code == 204
        assert api.get("/v1/news-items", headers=headers).status_code == 401


class TestStartingIsRefusedWhenItCannotFinish:
    def test_no_allowlist_is_a_503_before_anything_is_written(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection[Any]
    ) -> None:
        monkeypatch.delenv(ALLOWED_EMAILS_ENV)
        _, challenge = pkce()
        response = api.post("/v1/auth/native/start", json={"code_challenge": challenge})
        assert response.status_code == 503
        row = db.execute("SELECT count(*) AS n FROM oauth_states").fetchone()
        assert row is not None and row["n"] == 0

    def test_no_web_app_to_return_to_is_a_503(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MOTET_APP_BASE_URL")
        _, challenge = pkce()
        response = api.post("/v1/auth/native/start", json={"code_challenge": challenge})
        assert response.status_code == 503
        assert "MOTET_APP_BASE_URL" in response.json()["detail"]

    @pytest.mark.parametrize("challenge", ["plain", "x" * 42, "x" * 44, "a+b/" + "c" * 39])
    def test_only_an_s256_challenge_is_accepted(self, api: TestClient, challenge: str) -> None:
        response = api.post("/v1/auth/native/start", json={"code_challenge": challenge})
        assert response.status_code == 422


class TestTheUniversalLink:
    """With MOTET_IOS_APP_LINK set, the handoff travels on the web app's own https path.

    Approved by Tadas on 2026-09-13 as the stronger answer to the warning the first
    review raised: Apple only lets a sign-in sheet wait for an https callback on a host
    the app carries an associated-domains entitlement for, so no other app on the phone
    can receive the link. The flag is a claim about the *web app* serving an
    app-site-association file, which is why it is off unless an environment sets it.
    """

    @pytest.fixture
    def linked(self, api: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("MOTET_IOS_APP_LINK", "1")
        reset_store()
        return api

    def test_the_app_is_told_to_wait_for_the_web_apps_host(self, linked: TestClient) -> None:
        _, challenge = pkce()
        started = start(linked, challenge, app_link_domain="app.example.invalid")
        assert started["callback_host"] == "app.example.invalid"
        assert started["callback_path"] == "/app/signed-in"
        # The scheme is still reported: an iOS older than 17.4 cannot wait for an https
        # callback at all, and falls back to it.
        assert started["callback_scheme"] == "motet"

    def test_the_handoff_lands_on_that_link_and_still_redeems(self, linked: TestClient) -> None:
        verifier, challenge = pkce()
        login = finish_in_the_web_app(
            linked, start(linked, challenge, app_link_domain="app.example.invalid")
        )
        link = urlsplit(login["handoff_url"])
        assert (link.scheme, link.netloc, link.path) == (
            "https",
            "app.example.invalid",
            "/app/signed-in",
        )
        assert set(parse_qs(link.query)) == {"code"}

        code = parse_qs(link.query)["code"][0]
        assert redeem(linked, code, verifier).status_code == 200

    def test_an_app_that_cannot_take_the_link_gets_the_scheme(self, linked: TestClient) -> None:
        """The flag is on and the app says it cannot receive an https handoff.

        An iOS older than 17.4, or any build whose entitlement does not name this host —
        which is every build made before the deployment set MOTET_IOS_APP_DOMAIN. The
        sign-in sheet is watching for `motet://`, and a deployment that answered from its
        own flag alone would navigate to an https link the sheet never intercepts, leaving
        it open forever. This is the regression test for that.
        """
        verifier, challenge = pkce()
        started = start(linked, challenge)
        assert started["callback_host"] is None
        assert started["callback_path"] is None

        login = finish_in_the_web_app(linked, started)
        assert login["handoff_url"].startswith("motet://signed-in?")
        assert redeem(linked, handoff_code(login), verifier).status_code == 200

    def test_an_app_naming_another_host_gets_the_scheme(self, linked: TestClient) -> None:
        """Agreement is on one host, not on the idea of one."""
        _, challenge = pkce()
        started = start(linked, challenge, app_link_domain="somewhere.else.invalid")
        assert started["callback_host"] is None
        assert finish_in_the_web_app(linked, started)["handoff_url"].startswith("motet://")

    def test_a_web_app_that_cannot_carry_it_falls_back(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Callback.https(host:path:)` has nowhere to put a scheme or a port.

        So an http origin, or one carrying an explicit port, cannot be waited for however
        the flag is set — and answering with one would hang the sheet.
        """
        monkeypatch.setenv("MOTET_IOS_APP_LINK", "1")
        monkeypatch.setenv("MOTET_APP_BASE_URL", "http://localhost:5173")
        reset_store()
        _, challenge = pkce()
        started = start(api, challenge, app_link_domain="localhost")
        assert started["callback_host"] is None
        assert finish_in_the_web_app(api, started)["handoff_url"].startswith("motet://")

    def test_the_flag_off_is_the_custom_scheme(self, api: TestClient) -> None:
        """The default, and what every deployment gets until its web app serves the file."""
        _, challenge = pkce()
        started = start(api, challenge, app_link_domain="app.example.invalid")
        assert started["callback_host"] is None
        assert started["callback_path"] is None
        login = finish_in_the_web_app(api, started)
        assert login["handoff_url"].startswith("motet://signed-in?")
