"""Google Sign-In: the flow, the allowlist that is the actual lock, and the verifier.

Three layers, tested separately because they fail differently:

* **The routes**, through ``TestClient`` against a real Postgres and the fake identity
  provider — the round trip a browser makes, and every way it is refused.
* **The allowlist**, as plain functions. It is four lines and it is the whole
  authorization decision, so it gets its own tests rather than being incidentally
  exercised.
* **The ID token verifier**, against tokens signed by a key this module generates. That
  is the part that would be catastrophic to get wrong — a verifier that accepts a forged
  token authenticates the attacker instead of crashing — and invariant 7 means it has to
  be provable without calling Google. A locally generated RSA key is how: the tokens are
  real RS256 JWTs, verified by the real code path, with the JWKS lookup injected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import (
    ALLOWED_EMAILS_ENV,
    FAKE_EMAIL,
    LOGIN_STATE_PREFIX,
    GoogleIdentityProvider,
    IdentityError,
    allowed_emails,
    is_allowed,
)
from motet_api.config import CALLBACK_PATH
from motet_api.deps import reset_store
from motet_db import auth as auth_repo
from motet_db import repo

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
APP_ORIGIN = "https://app.example.invalid"
REDIRECT = f"{APP_ORIGIN}{CALLBACK_PATH}"


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


def sign_in(api: TestClient, redirect_uri: str = REDIRECT) -> dict[str, Any]:
    """Walk the whole flow the way a browser does, and return the login response.

    The fake provider's authorization URL redirects straight back to ``redirect_uri`` with
    a code and the state — which is what a real provider does once a human has clicked
    allow — so following it here is the same round trip, minus the human.
    """
    started = api.post("/v1/auth/google/start", json={"redirect_uri": redirect_uri})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    completed = api.post(
        "/v1/auth/google/callback",
        json={"state": query["state"][0], "code": query["code"][0]},
    )
    assert completed.status_code == 200, completed.text
    body: dict[str, Any] = completed.json()
    return body


class TestSigningIn:
    def test_a_signed_in_browser_gets_a_token_that_works_on_v1(self, api: TestClient) -> None:
        """The whole point: a session token is a bearer token, so nothing else changes.

        The SPA already sends `Authorization: Bearer`, so signing in changes where the
        value comes from and not one line of how a request is made.
        """
        login = sign_in(api)
        assert login["email"] == FAKE_EMAIL

        headers = {"Authorization": f"Bearer {login['token']}"}
        assert api.get("/v1/news-items", headers=headers).status_code == 200

    def test_the_session_route_says_how_the_caller_proved_itself(self, api: TestClient) -> None:
        login = sign_in(api)

        as_session = api.get(
            "/v1/auth/session", headers={"Authorization": f"Bearer {login['token']}"}
        ).json()
        assert as_session["how"] == "session"
        assert as_session["email"] == FAKE_EMAIL
        assert as_session["expires_at"] is not None

        as_token = api.get("/v1/auth/session", headers=AUTH).json()
        assert as_token["how"] == "token"
        # The shared token belongs to no person, so there is no address to report.
        assert as_token["email"] is None

    def test_the_api_token_keeps_working(self, api: TestClient) -> None:
        """The bearer path is not replaced. The feed, the iOS app and every script use it.

        What changes is that a *human* stops typing it into a browser — not that the
        system stops accepting it.
        """
        assert api.get("/v1/news-items", headers=AUTH).status_code == 200

    def test_logout_actually_revokes(self, api: TestClient) -> None:
        """Server-side rows are what make this true; a signed token could not be."""
        login = sign_in(api)
        headers = {"Authorization": f"Bearer {login['token']}"}
        assert api.get("/v1/news-items", headers=headers).status_code == 200

        assert api.post("/v1/auth/logout", headers=headers).status_code == 204
        assert api.get("/v1/news-items", headers=headers).status_code == 401

    def test_logout_with_the_shared_token_is_a_no_op_rather_than_an_error(
        self, api: TestClient
    ) -> None:
        """So a client can sign out without first working out what kind of token it holds."""
        assert api.post("/v1/auth/logout", headers=AUTH).status_code == 204
        assert api.get("/v1/news-items", headers=AUTH).status_code == 200

    def test_an_expired_session_stops_working_immediately(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Expiry is enforced on read, not by a sweep that has to get round to it."""
        login = sign_in(api)
        headers = {"Authorization": f"Bearer {login['token']}"}
        db.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 second'")
        db.commit()
        assert api.get("/v1/news-items", headers=headers).status_code == 401

    def test_the_token_is_stored_only_as_a_hash(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Nothing ever needs to read this value back, so nothing keeps it."""
        login = sign_in(api)
        rows = db.execute("SELECT token_sha256 FROM auth_sessions").fetchall()
        stored = [row["token_sha256"] for row in rows]
        assert login["token"] not in stored
        assert stored == [auth_repo.token_digest(login["token"])]


class TestTheAllowlistIsTheLock:
    def test_an_unset_allowlist_denies_rather_than_allows(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed.

        This consent screen is published and unverified, so anyone with a Google account
        can complete the flow. An allowlist that defaulted to "everybody" would make this
        a strictly worse door than the shared token it replaces.
        """
        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        assert started.status_code == 503
        assert ALLOWED_EMAILS_ENV in started.json()["detail"]

    def test_a_verified_identity_that_is_not_on_the_list_is_refused(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection[Any]
    ) -> None:
        """Google says who; the allowlist says whether.

        The sign-in is started while the fake's address *is* allowed and completed after
        the list changes, which is how the refusal lands on the identity check rather than
        on the earlier "sign-in is switched off" guard.
        """
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)

        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "somebody-else@example.invalid")
        refused = api.post(
            "/v1/auth/google/callback",
            json={"state": query["state"][0], "code": query["code"][0]},
        )
        assert refused.status_code == 403
        counted = db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()
        assert counted is not None and counted["n"] == 0

    def test_removing_an_address_revokes_the_sessions_it_already_had(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection[Any]
    ) -> None:
        """The allowlist is an ongoing control, not a one-time gate at the door.

        Checked at sign-in only, taking somebody off the list would revoke nothing for up
        to the session's whole 30-day life — and there would be no lever to do it with,
        since `/v1/auth/logout` needs the token being revoked and invariant 10 says nobody
        has a shell to run a DELETE from.
        """
        login = sign_in(api)
        headers = {"Authorization": f"Bearer {login['token']}"}
        assert api.get("/v1/news-items", headers=headers).status_code == 200

        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "somebody-else@example.invalid")
        assert api.get("/v1/news-items", headers=headers).status_code == 401
        # The row is gone, not merely refused: putting the address back must not silently
        # restore a session somebody was removed from.
        counted = db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()
        assert counted is not None and counted["n"] == 0

    def test_revoking_everywhere_takes_out_a_session_on_another_device(
        self, api: TestClient
    ) -> None:
        """The answer to a lost phone, reachable from a device you still have."""
        lost = sign_in(api)
        kept = sign_in(api)

        revoked = api.post("/v1/auth/logout-all", headers=AUTH)
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] == 2
        for session in (lost, kept):
            headers = {"Authorization": f"Bearer {session['token']}"}
            assert api.get("/v1/news-items", headers=headers).status_code == 401

    def test_fake_mode_cannot_mint_a_session_for_a_real_deployment(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fake identity provider is the *default*, so this is the guard that matters.

        A deployment that forgot `MOTET_INFERENCE_MODE=real` would hand out verified
        identities with no Google involved at all. It is closed anyway, and this is why:
        the fake answers as a `.test` address (RFC 6761 — never a real mailbox), so it can
        only get in on a deployment whose allowlist literally names it.
        """
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "a-real-person@example.invalid")
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)

        refused = api.post(
            "/v1/auth/google/callback",
            json={"state": query["state"][0], "code": query["code"][0]},
        )
        assert refused.status_code == 403

    def test_health_reports_whether_anyone_could_sign_in(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same spirit as `authenticated`: a login that denies silently looks fine."""
        assert api.get("/internal/health").json()["login_configured"] is True
        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        assert api.get("/internal/health").json()["login_configured"] is False

    def test_real_mode_without_an_oauth_client_is_not_configured(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An allowlist alone is not a working sign-in if the flow cannot start."""
        monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        assert api.get("/internal/health").json()["login_configured"] is False


class TestTheHandshake:
    def test_the_state_is_tagged_as_a_sign_in(self, api: TestClient) -> None:
        """Both flows land on the SPA's one /oauth/callback path.

        `state` is the only value guaranteed to survive the round trip through the
        provider, so the flow is encoded in it — and the dot is safe because
        `secrets.token_urlsafe` emits only `[A-Za-z0-9_-]`.
        """
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        assert started.json()["state"].startswith(LOGIN_STATE_PREFIX)

    def test_a_mailbox_state_is_refused_by_the_sign_in_callback(self, api: TestClient) -> None:
        connect = api.post(
            "/v1/sources/connect",
            json={"provider": "gmail", "name": "Gmail", "redirect_uri": REDIRECT},
            headers=AUTH,
        )
        assert connect.status_code == 201
        refused = api.post(
            "/v1/auth/google/callback",
            json={"state": connect.json()["state"], "code": "whatever"},
        )
        assert refused.status_code == 400

    def test_a_sign_in_state_is_refused_by_the_mailbox_callback(self, api: TestClient) -> None:
        """And the other way round, so neither flow can spend the other's authorization."""
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        refused = api.post(
            "/v1/sources/callback",
            json={"state": started.json()["state"], "code": "whatever"},
            headers=AUTH,
        )
        assert refused.status_code == 400

    def test_a_state_is_good_for_exactly_one_callback(self, api: TestClient) -> None:
        """`DELETE ... RETURNING`, so a replay finds nothing rather than minting a second
        session."""
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        payload = {"state": query["state"][0], "code": query["code"][0]}

        assert api.post("/v1/auth/google/callback", json=payload).status_code == 200
        assert api.post("/v1/auth/google/callback", json=payload).status_code == 400

    def test_a_sign_in_state_survives_being_sent_to_the_wrong_route(self, api: TestClient) -> None:
        """Refused *before* the consume, so the authorization is still spendable.

        Checking the provider on the consumed row would be too late: consuming is what
        destroys it, and the user would start again for no visible reason.
        """
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)

        misdelivered = api.post(
            "/v1/sources/callback",
            json={"state": query["state"][0], "code": query["code"][0]},
            headers=AUTH,
        )
        assert misdelivered.status_code == 400

        # And now the same authorization still completes, at the route that owns it.
        completed = api.post(
            "/v1/auth/google/callback",
            json={"state": query["state"][0], "code": query["code"][0]},
        )
        assert completed.status_code == 200

    def test_an_unset_allowlist_does_not_burn_the_authorization(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 503 means "come back when this is configured", so the code must survive it."""
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
        payload = {"state": query["state"][0], "code": query["code"][0]}

        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        assert api.post("/v1/auth/google/callback", json=payload).status_code == 503

        monkeypatch.setenv(ALLOWED_EMAILS_ENV, FAKE_EMAIL)
        assert api.post("/v1/auth/google/callback", json=payload).status_code == 200

    def test_an_unknown_state_is_refused(self, api: TestClient) -> None:
        response = api.post(
            "/v1/auth/google/callback",
            json={"state": f"{LOGIN_STATE_PREFIX}never-issued", "code": "abc"},
        )
        assert response.status_code == 400

    def test_the_pending_authorization_records_the_nonce(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """OIDC's replay defence only means something if what was sent is remembered.

        That it is also *sent* is asserted against the real adapter below, not here: the
        fake provider redirects straight back with a code and a state and renders no
        consent URL to read a nonce out of.
        """
        started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
        row = db.execute(
            "SELECT provider, source_id, nonce FROM oauth_states WHERE state = %s",
            (started.json()["state"],),
        ).fetchone()
        assert row is not None
        assert row["provider"] == "google"
        # A sign-in connects nothing. The column is nullable for exactly this.
        assert row["source_id"] is None
        assert row["nonce"]

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://evil.example.invalid/oauth/callback",
            f"{APP_ORIGIN}/somewhere-else",
            "not a url at all",
        ],
    )
    def test_a_redirect_uri_that_is_not_this_deployments_callback_is_refused(
        self, api: TestClient, redirect_uri: str
    ) -> None:
        """Google is the real check — it matches registered URIs by exact string.

        This is here because *starting* a sign-in has to be unauthenticated, and an
        unauthenticated route that echoes an arbitrary caller-supplied URL back out is a
        shape worth not having even when it is provably harmless.
        """
        response = api.post("/v1/auth/google/start", json={"redirect_uri": redirect_uri})
        assert response.status_code == 400


class TestTheSessionRoutesAreThemselvesGuarded:
    def test_session_and_logout_refuse_an_unauthenticated_caller(self, api: TestClient) -> None:
        assert api.get("/v1/auth/session").status_code == 401
        assert api.post("/v1/auth/logout").status_code == 401
        assert api.post("/v1/auth/logout-all").status_code == 401

    def test_a_non_ascii_bearer_is_a_401_rather_than_a_500(self, api: TestClient) -> None:
        """Starlette decodes headers as latin-1 and `compare_digest` rejects non-ASCII str.

        Without the guard this is an unhandled TypeError — a 500, and a reported error,
        from an unauthenticated request anybody can make.
        """
        assert api.get("/v1/news-items", headers={"Authorization": "Bearer é"}).status_code == 401

    def test_an_unlocked_deployment_says_so_rather_than_pretending(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`how: 'open'` is what lets a laptop's SPA skip a sign-in door it cannot pass.

        A browser cannot tell "I have no credential" from "no credential is needed"
        without asking, and a door in front of an API that is already answering is a dead
        end — the button 503s, because a laptop has no allowlist either.
        """
        monkeypatch.delenv("MOTET_API_TOKEN", raising=False)
        body = api.get("/v1/auth/session").json()
        assert body["how"] == "open"
        assert body["email"] is None


class TestAllowlistParsing:
    def test_unset_means_nobody(self) -> None:
        assert allowed_emails({}) == frozenset()
        assert is_allowed("anyone@example.com", frozenset()) is False

    def test_addresses_are_compared_case_insensitively(self) -> None:
        allowed = allowed_emails({ALLOWED_EMAILS_ENV: "Tadas@Example.COM"})
        assert is_allowed("tadas@example.com", allowed) is True
        assert is_allowed("  TADAS@EXAMPLE.COM ", allowed) is True

    def test_a_list_is_split_on_commas_and_trimmed(self) -> None:
        allowed = allowed_emails({ALLOWED_EMAILS_ENV: " a@x.test , b@y.test ,, "})
        assert allowed == frozenset({"a@x.test", "b@y.test"})

    def test_gmail_aliases_are_not_folded_in(self) -> None:
        """Folding dots and `+` tags would silently *widen* the list past what was written."""
        allowed = allowed_emails({ALLOWED_EMAILS_ENV: "first.last@gmail.com"})
        assert is_allowed("firstlast@gmail.com", allowed) is False
        assert is_allowed("first.last+motet@gmail.com", allowed) is False


# --- the ID token verifier -------------------------------------------------------------

CLIENT_ID = "motet-oauth-client.apps.googleusercontent.invalid"
NONCE = "the-nonce-we-sent"


@pytest.fixture(scope="module")
def signing_key() -> Any:
    """An RSA key standing in for Google's.

    Generated rather than fetched: invariant 7 says no test in this repo makes a real
    vendor call, and a verifier tested against a hardcoded token would expire.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def id_token(signing_key: Any, **overrides: Any) -> str:
    import jwt

    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "owner@example.test",
        "email_verified": True,
        "name": "The Owner",
        "nonce": NONCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    claims.update(overrides)
    return str(jwt.encode(claims, signing_key, algorithm="RS256"))


def provider(signing_key: Any) -> GoogleIdentityProvider:
    """The real adapter, with the JWKS lookup injected and no transport.

    Everything below calls ``_verify`` directly, which is the half that decides whether a
    token is believed; the exchange half is covered separately with a stub transport.
    """
    return GoogleIdentityProvider(
        client_id=CLIENT_ID,
        client_secret="not-used-here",
        signing_key=signing_key.public_key(),
    )


class TestIdTokenVerification:
    def test_a_well_formed_token_verifies(self, signing_key: Any) -> None:
        claims = provider(signing_key)._verify(id_token(signing_key), nonce=NONCE)
        assert claims["email"] == "owner@example.test"

    def test_a_token_signed_by_someone_else_is_refused(self, signing_key: Any) -> None:
        """The check that matters most: anyone can mint claims, only Google can sign them."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = id_token(impostor)
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(forged, nonce=NONCE)

    def test_a_token_for_another_application_is_refused(self, signing_key: Any) -> None:
        """A valid Google token about a real person is still not a token about *our* user."""
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(
                id_token(signing_key, aud="some-other-app.apps.googleusercontent.invalid"),
                nonce=NONCE,
            )

    def test_a_token_from_another_issuer_is_refused(self, signing_key: Any) -> None:
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(
                id_token(signing_key, iss="https://accounts.evil.invalid"), nonce=NONCE
            )

    def test_an_expired_token_is_refused(self, signing_key: Any) -> None:
        stale = datetime.now(tz=UTC) - timedelta(hours=1)
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(
                id_token(signing_key, exp=int(stale.timestamp())), nonce=NONCE
            )

    def test_a_token_with_no_expiry_is_refused(self, signing_key: Any) -> None:
        """Missing must not read as "never expires"."""
        import jwt

        now = int(datetime.now(tz=UTC).timestamp())
        token = str(
            jwt.encode(
                {
                    "iss": "https://accounts.google.com",
                    "aud": CLIENT_ID,
                    "sub": "1",
                    "email": "owner@example.test",
                    "email_verified": True,
                    "nonce": NONCE,
                    "iat": now,
                },
                signing_key,
                algorithm="RS256",
            )
        )
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(token, nonce=NONCE)

    def test_a_token_from_a_different_sign_in_is_refused(self, signing_key: Any) -> None:
        """What the nonce is for: a token captured elsewhere cannot be replayed in here."""
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(
                id_token(signing_key, nonce="someone-elses-nonce"), nonce=NONCE
            )

    def test_a_missing_expectation_fails_rather_than_disabling_the_check(
        self, signing_key: Any
    ) -> None:
        """`nonce=""` must not mean "skip the nonce".

        The column is nullable because the Gmail flow has no ID token to bind, so a
        sign-in row that somehow carried no nonce would otherwise switch OIDC's replay
        defence off silently — a fail-open inside the one function whose job is failing
        closed.
        """
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(id_token(signing_key), nonce="")

    def test_an_unverified_address_is_not_proof_of_anything(self, signing_key: Any) -> None:
        """`email_verified: false` means Google is repeating a string somebody typed.

        Authorizing on it would let anyone put an allowlisted address on their own
        account and walk in.
        """
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(id_token(signing_key, email_verified=False), nonce=NONCE)

    def test_the_algorithm_cannot_be_talked_down(self, signing_key: Any) -> None:
        """`alg: none` is the oldest JWT attack there is. RS256 is the only one accepted."""
        import jwt

        unsigned = str(
            jwt.encode(
                {
                    "iss": "https://accounts.google.com",
                    "aud": CLIENT_ID,
                    "sub": "1",
                    "email": "owner@example.test",
                    "email_verified": True,
                    "nonce": NONCE,
                    "iat": int(datetime.now(tz=UTC).timestamp()),
                    "exp": int((datetime.now(tz=UTC) + timedelta(minutes=5)).timestamp()),
                },
                key="",
                algorithm="none",
            )
        )
        with pytest.raises(IdentityError):
            provider(signing_key)._verify(unsigned, nonce=NONCE)


class _StubResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


class _StubTransport:
    def __init__(self, response: _StubResponse) -> None:
        self._response = response
        self.form: dict[str, str] | None = None

    def post(self, url: str, data: dict[str, str]) -> _StubResponse:
        self.form = data
        return self._response


class TestTheRealExchange:
    def test_a_verified_identity_comes_back_out_of_a_code(self, signing_key: Any) -> None:
        transport = _StubTransport(_StubResponse(200, {"id_token": id_token(signing_key)}))
        google = GoogleIdentityProvider(
            client_id=CLIENT_ID,
            client_secret="a-secret",
            signing_key=signing_key.public_key(),
            transport=transport,
        )

        identity = google.complete(
            code="the-code", redirect_uri=REDIRECT, code_verifier="the-verifier", nonce=NONCE
        )

        assert identity.email == "owner@example.test"
        assert identity.name == "The Owner"
        assert transport.form is not None
        assert transport.form["grant_type"] == "authorization_code"
        # PKCE, even though this is a confidential client: it costs one parameter and
        # closes the window on an intercepted code.
        assert transport.form["code_verifier"] == "the-verifier"

    def test_a_response_with_no_id_token_says_which_scope_is_missing(
        self, signing_key: Any
    ) -> None:
        """The likely misconfiguration: an OAuth client that was never given `openid`."""
        transport = _StubTransport(_StubResponse(200, {"access_token": "at"}))
        google = GoogleIdentityProvider(client_id=CLIENT_ID, client_secret="s", transport=transport)
        with pytest.raises(IdentityError, match="openid"):
            google.complete(code="c", redirect_uri=REDIRECT, code_verifier="v", nonce=NONCE)

    def test_a_rejected_code_is_reported_without_echoing_it(self, signing_key: Any) -> None:
        transport = _StubTransport(
            _StubResponse(400, {"error": "invalid_grant", "error_description": "Bad code"})
        )
        google = GoogleIdentityProvider(client_id=CLIENT_ID, client_secret="s", transport=transport)
        with pytest.raises(IdentityError) as caught:
            google.complete(
                code="the-secret-code", redirect_uri=REDIRECT, code_verifier="v", nonce=NONCE
            )
        assert "the-secret-code" not in str(caught.value)

    def test_the_authorization_url_asks_for_identity_and_nothing_else(
        self, signing_key: Any
    ) -> None:
        """Not the Gmail flow's parameters.

        Signing in needs no refresh token and no forced consent screen — those exist so a
        *mailbox* connection survives, and re-prompting every sign-in would be friction
        with no security value.
        """
        google = GoogleIdentityProvider(client_id=CLIENT_ID, client_secret="s")
        query = parse_qs(
            urlsplit(
                google.authorization_url(
                    redirect_uri=REDIRECT, state="login.abc", nonce=NONCE, code_challenge="chal"
                )
            ).query
        )
        assert query["scope"] == ["openid email profile"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["nonce"] == [NONCE]
        assert "access_type" not in query
        assert query["prompt"] == ["select_account"]


def test_the_login_state_prefix_is_the_literal_the_spa_also_hardcodes() -> None:
    """`web/src/oauth.ts` has its own copy, and there is no way to share one.

    Pinned on both sides so that changing it here without changing it there fails a test
    rather than routing every sign-in callback into the mailbox handler.
    """
    assert LOGIN_STATE_PREFIX == "login."
    # The marker has to be a character `secrets.token_urlsafe` cannot emit, or a mailbox
    # state could accidentally look like a sign-in one.
    assert not LOGIN_STATE_PREFIX[-1].isalnum()
    assert LOGIN_STATE_PREFIX[-1] not in "-_"


class TestSessionsAreOneAccount:
    def test_every_session_belongs_to_the_one_owner(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Signing in does not create a user, and this is the assertion that says so.

        Motet has exactly one account. If this ever fails, somebody has built the user
        system that AGENTS.md still lists as out of scope.
        """
        sign_in(api)
        owners = db.execute("SELECT DISTINCT user_id FROM auth_sessions").fetchall()
        assert [row["user_id"] for row in owners] == [repo.OWNER_USER_ID]
