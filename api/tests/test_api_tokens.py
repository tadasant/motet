"""Personal access tokens: the non-interactive way into ``/v1`` (Tadas, 2026-09-20).

Four things are being held here, and they fail in different ways:

* **The round trip.** Sign in, mint, use the token on a real route, revoke, and watch it
  stop working — against a real Postgres, because the expiry and the revocation are
  predicates in SQL rather than branches in Python.
* **What lands in the database.** The plaintext must exist in exactly one response body
  and nowhere else, so a test reads the row and asserts the token is not in it.
* **What a token may not do.** It is not an operator, and it cannot mint or revoke
  another token. Neither can the shared API token, and neither can an MCP client's grant
  — that last one is the escalation that is easy to miss, because an MCP access token
  *is* an ``auth_sessions`` row.
* **What keeps it out of an error report.** ``sentry_sdk`` redacts a frame local by its
  *name*, so the auth path calls its credential ``token`` and ``secret``. That is an
  assumption about somebody else's denylist, so it is pinned against the installed SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV, FAKE_EMAIL
from motet_api.config import CALLBACK_PATH, DEFAULT_TOKEN_LABEL, TOKEN_LABEL_ENV, token_label
from motet_api.deps import reset_store
from motet_api.throttle import MAX_FAILURES, FailureThrottle
from motet_db import api_tokens as token_repo
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
    # The owner *is* an operator here, so "a token is not an admin" is a statement about
    # the token rather than about a deployment that has no admins.
    monkeypatch.setenv(ADMIN_EMAILS_ENV, FAKE_EMAIL)
    monkeypatch.setenv(TOKEN_LABEL_ENV, "stg")
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def session_token(api: TestClient) -> str:
    """Sign in the way a browser does and return the session token it is handed."""
    started = api.post("/v1/auth/google/start", json={"redirect_uri": REDIRECT})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    completed = api.post(
        "/v1/auth/google/callback",
        json={"state": query["state"][0], "code": query["code"][0]},
    )
    assert completed.status_code == 200, completed.text
    return str(completed.json()["token"])


def signed_in(api: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {session_token(api)}"}


def mint(
    api: TestClient,
    headers: dict[str, str],
    label: str = "staging agent",
    expires_in_days: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"label": label}
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    response = api.post("/v1/auth/tokens", json=body, headers=headers)
    assert response.status_code == 201, response.text
    minted: dict[str, Any] = response.json()
    return minted


class TestMintingAndUsing:
    def test_a_minted_token_authenticates_a_real_request(self, api: TestClient) -> None:
        """The whole point: a PAT is a bearer token, so no call site changes."""
        minted = mint(api, signed_in(api))
        headers = {"Authorization": f"Bearer {minted['token']}"}

        who = api.get("/v1/auth/session", headers=headers)
        assert who.status_code == 200, who.text
        assert who.json()["how"] == "pat"
        assert who.json()["email"] == FAKE_EMAIL

        # A route that reads data, not just the one that describes the caller.
        backlog = api.get("/v1/news-items", headers=headers)
        assert backlog.status_code == 200, backlog.text

    def test_the_token_carries_the_environment_in_its_prefix(self, api: TestClient) -> None:
        minted = mint(api, signed_in(api))
        assert minted["token"].startswith("mot_stg_")
        assert minted["created"]["prefix"].startswith("mot_stg_")
        # Display only, and deliberately far short of the secret.
        assert minted["token"].startswith(minted["created"]["prefix"])
        assert len(minted["created"]["prefix"]) < len(minted["token"])

    def test_a_token_can_write_as_well_as_read(self, api: TestClient) -> None:
        """A PAT resolves to the same user, so it may spend that user's pipeline."""
        headers = {"Authorization": f"Bearer {mint(api, signed_in(api))['token']}"}
        pasted = api.post(
            "/v1/sources/paste", json={"title": "A thing", "text": "x" * 80}, headers=headers
        )
        assert pasted.status_code == 201, pasted.text

    def test_a_session_token_that_looks_like_a_pat_still_works(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The marker routes the probe; it must not commit to the answer.

        A session token is 43 random url-safe characters and one in 16.7 million begins
        ``mot_``. Committing to the PAT table on the marker alone would refuse that person
        on every request until they signed in again, and nothing would say why.
        """
        secret = "mot_" + auth_repo.new_session_token()
        auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=FAKE_EMAIL, token=secret)
        db.commit()

        who = api.get("/v1/auth/session", headers={"Authorization": f"Bearer {secret}"})
        assert who.status_code == 200, who.text
        assert who.json()["how"] == "session"

    def test_a_wrong_token_is_refused(self, api: TestClient) -> None:
        mint(api, signed_in(api))
        refused = api.get(
            "/v1/auth/session", headers={"Authorization": "Bearer mot_stg_not-a-real-token"}
        )
        assert refused.status_code == 401
        assert "access token" in refused.json()["detail"]

    def test_one_character_off_is_refused(self, api: TestClient) -> None:
        """The digest is the lookup, so a near miss is as wrong as any other string."""
        minted = mint(api, signed_in(api))
        nearly = minted["token"][:-1] + ("A" if minted["token"][-1] != "A" else "B")
        assert (
            api.get("/v1/auth/session", headers={"Authorization": f"Bearer {nearly}"}).status_code
            == 401
        )

    def test_a_revoked_token_stops_working_at_once(self, api: TestClient) -> None:
        owner = signed_in(api)
        minted = mint(api, owner)
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert api.get("/v1/auth/session", headers=headers).status_code == 200

        revoked = api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=owner)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked_at"] is not None

        assert api.get("/v1/auth/session", headers=headers).status_code == 401

    def test_an_expired_token_is_refused(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Expiry is a predicate, not a sweep, so a lapsed token stops immediately."""
        minted = mint(api, signed_in(api), expires_in_days=30)
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert api.get("/v1/auth/session", headers=headers).status_code == 200

        db.execute(
            "UPDATE api_tokens SET expires_at = now() - interval '1 second' WHERE id = %s",
            (minted["created"]["id"],),
        )
        db.commit()
        assert api.get("/v1/auth/session", headers=headers).status_code == 401

    def test_an_expiry_is_recorded_when_asked_for(self, api: TestClient) -> None:
        minted = mint(api, signed_in(api), expires_in_days=7)
        expires = datetime.fromisoformat(minted["created"]["expires_at"])
        assert timedelta(days=6) < expires - datetime.now(UTC) <= timedelta(days=7)

    def test_no_expiry_by_default(self, api: TestClient) -> None:
        assert mint(api, signed_in(api))["created"]["expires_at"] is None


class TestWhatIsStored:
    def test_the_database_holds_a_hash_and_never_the_token(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The claim the whole design rests on, asserted against the row itself."""
        minted = mint(api, signed_in(api))
        secret = minted["token"]

        row = db.execute(
            "SELECT * FROM api_tokens WHERE id = %s", (minted["created"]["id"],)
        ).fetchone()
        assert row is not None
        stored = dict(row)

        # Nothing anywhere in the row is the token, in any column.
        assert secret not in " ".join(str(value) for value in stored.values())
        assert stored["token_sha256"] == token_repo.token_digest(secret)
        # And the digest is not reversible to it: 64 lowercase hex characters.
        assert len(stored["token_sha256"]) == 64
        # The prefix is stored, and is a *prefix* rather than a slice of the secret's tail.
        assert secret.startswith(stored["prefix"])

    def test_the_token_is_returned_exactly_once(self, api: TestClient) -> None:
        owner = signed_in(api)
        minted = mint(api, owner)

        listed = api.get("/v1/auth/tokens", headers=owner)
        assert listed.status_code == 200, listed.text
        assert minted["token"] not in listed.text
        assert "token" not in listed.json()[0]

        revoked = api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=owner)
        assert minted["token"] not in revoked.text

    def test_last_used_at_is_written_on_first_use(self, api: TestClient) -> None:
        owner = signed_in(api)
        minted = mint(api, owner)
        assert minted["created"]["last_used_at"] is None

        api.get("/v1/auth/session", headers={"Authorization": f"Bearer {minted['token']}"})

        listed = api.get("/v1/auth/tokens", headers=owner).json()
        assert listed[0]["last_used_at"] is not None


class TestListingAndRevoking:
    def test_the_list_shows_prefix_label_created_and_last_used(self, api: TestClient) -> None:
        owner = signed_in(api)
        mint(api, owner, label="staging agent")

        listed = api.get("/v1/auth/tokens", headers=owner).json()
        assert len(listed) == 1
        assert listed[0]["label"] == "staging agent"
        assert listed[0]["email"] == FAKE_EMAIL
        assert set(listed[0]) == {
            "id",
            "prefix",
            "label",
            "email",
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked_at",
        }

    def test_a_revoked_token_stays_in_the_list(self, api: TestClient) -> None:
        """Audit: with no database shell, this list is where 'when did it stop' is asked."""
        owner = signed_in(api)
        minted = mint(api, owner)
        api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=owner)

        listed = api.get("/v1/auth/tokens", headers=owner).json()
        assert len(listed) == 1
        assert listed[0]["revoked_at"] is not None

    def test_revoking_twice_is_not_an_error(self, api: TestClient) -> None:
        owner = signed_in(api)
        minted = mint(api, owner)
        first = api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=owner)
        second = api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=owner)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["revoked_at"] == first.json()["revoked_at"]

    def test_revoking_an_unknown_token_is_a_404(self, api: TestClient) -> None:
        assert api.delete("/v1/auth/tokens/pat_nope", headers=signed_in(api)).status_code == 404

    def test_an_empty_label_is_refused(self, api: TestClient) -> None:
        assert (
            api.post("/v1/auth/tokens", json={"label": ""}, headers=signed_in(api)).status_code
            == 422
        )


class TestWhatATokenMayNotDo:
    def test_a_token_cannot_mint_another_token(self, api: TestClient) -> None:
        """A token is a leaf: revoking the one you know about leaves no descendants."""
        headers = {"Authorization": f"Bearer {mint(api, signed_in(api))['token']}"}
        refused = api.post("/v1/auth/tokens", json={"label": "child"}, headers=headers)
        assert refused.status_code == 403
        assert "cannot manage access tokens" in refused.json()["detail"]

    def test_a_token_cannot_list_or_revoke_tokens(self, api: TestClient) -> None:
        owner = signed_in(api)
        minted = mint(api, owner)
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert api.get("/v1/auth/tokens", headers=headers).status_code == 403
        assert (
            api.delete(f"/v1/auth/tokens/{minted['created']['id']}", headers=headers).status_code
            == 403
        )

    def test_the_shared_api_token_cannot_mint_one(self, api: TestClient) -> None:
        """Rotating MOTET_API_TOKEN is its own recovery; a PAT would survive it."""
        refused = api.post(
            "/v1/auth/tokens", json={"label": "from the shared secret"}, headers=AUTH
        )
        assert refused.status_code == 403
        assert "shared API token" in refused.json()["detail"]

    def test_an_mcp_grant_cannot_mint_one(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The escalation that is easy to miss: an MCP access token *is* a session row."""
        db.execute(
            "INSERT INTO mcp_oauth_clients (client_id, client_info) VALUES (%s, %s)",
            ("client-1", "{}"),
        )
        secret = auth_repo.new_session_token()
        auth_repo.create_session(
            db,
            user_id=repo.OWNER_USER_ID,
            email=FAKE_EMAIL,
            token=secret,
            mcp_client_id="client-1",
        )
        db.commit()
        headers = {"Authorization": f"Bearer {secret}"}
        # The grant is a working credential — this is not a test of a broken session.
        assert api.get("/v1/auth/session", headers=headers).status_code == 200

        refused = api.post("/v1/auth/tokens", json={"label": "escalation"}, headers=headers)
        assert refused.status_code == 403
        assert "MCP client" in refused.json()["detail"]

    def test_a_token_is_never_an_operator(self, api: TestClient) -> None:
        headers = {"Authorization": f"Bearer {mint(api, signed_in(api))['token']}"}
        assert api.get("/v1/auth/session", headers=headers).json()["admin"] is False
        refused = api.get("/v1/admin/overview", headers=headers)
        assert refused.status_code == 403
        assert "personal access token is not one" in refused.json()["detail"]

    def test_a_token_dies_when_its_address_leaves_the_allowlist(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection[Any]
    ) -> None:
        """De-listed has to mean gone, and a PAT outlives the session that minted it."""
        minted = mint(api, signed_in(api))
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert api.get("/v1/auth/session", headers=headers).status_code == 200

        monkeypatch.setenv(ALLOWED_EMAILS_ENV, "somebody-else@motet.test")
        assert api.get("/v1/auth/session", headers=headers).status_code == 401

        # Revoked rather than left to be refused on every request, and the revocation
        # survives the rolled-back request that discovered it.
        row = db.execute(
            "SELECT revoked_at FROM api_tokens WHERE id = %s", (minted["created"]["id"],)
        ).fetchone()
        assert row is not None and row["revoked_at"] is not None

    def test_the_feed_token_is_not_an_api_token(self, api: TestClient) -> None:
        """A podcast URL's secret must not become a key to /v1 — unchanged, pinned here."""
        feed = api.get("/v1/feed", headers=AUTH)
        assert feed.status_code == 200, feed.text
        token = feed.json()["token"]
        assert (
            api.get("/v1/auth/session", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


class TestTheExistingPathsAreUnchanged:
    def test_the_shared_token_still_works(self, api: TestClient) -> None:
        assert api.get("/v1/auth/session", headers=AUTH).json()["how"] == "token"

    def test_a_session_still_works(self, api: TestClient) -> None:
        assert api.get("/v1/auth/session", headers=signed_in(api)).json()["how"] == "session"

    def test_logout_all_does_not_touch_tokens(self, api: TestClient) -> None:
        """Deliberate: signing out everywhere must not kill the agent's credential."""
        owner = signed_in(api)
        minted = mint(api, owner)
        assert api.post("/v1/auth/logout-all", headers=AUTH).status_code == 200
        assert (
            api.get(
                "/v1/auth/session", headers={"Authorization": f"Bearer {minted['token']}"}
            ).status_code
            == 200
        )

    def test_a_token_cannot_sign_the_owner_out(self, api: TestClient) -> None:
        """A leaked PAT must not be able to out-race its own revocation.

        `logout-all` deletes every session, and a session is the only credential that can
        reach the revoke route — so a PAT able to call it could delete the owner's session
        on a loop and never be revoked.
        """
        owner = signed_in(api)
        headers = {"Authorization": f"Bearer {mint(api, owner)['token']}"}
        refused = api.post("/v1/auth/logout-all", headers=headers)
        assert refused.status_code == 403
        assert "cannot sign other sessions out" in refused.json()["detail"]
        # The owner's session is untouched, so the revoke lever still works.
        assert api.get("/v1/auth/tokens", headers=owner).status_code == 200

    def test_the_shared_token_can_still_sign_every_session_out(self, api: TestClient) -> None:
        """The property `logout-all` exists for: reachable from a *different* device."""
        signed_in(api)
        assert api.post("/v1/auth/logout-all", headers=AUTH).json()["revoked"] >= 1


class TestTheFailedAuthThrottle:
    def test_repeated_failures_become_a_429_with_retry_after(self, api: TestClient) -> None:
        bad = {"Authorization": "Bearer mot_stg_wrong"}
        for _ in range(MAX_FAILURES):
            assert api.get("/v1/auth/session", headers=bad).status_code == 401
        throttled = api.get("/v1/auth/session", headers=bad)
        assert throttled.status_code == 429
        assert throttled.headers["Retry-After"] == "60"

    def test_a_valid_credential_is_never_throttled(self, api: TestClient) -> None:
        """The property that makes this safe: an attacker cannot lock the owner out."""
        headers = {"Authorization": f"Bearer {mint(api, signed_in(api))['token']}"}
        for _ in range(MAX_FAILURES * 2):
            api.get("/v1/auth/session", headers={"Authorization": "Bearer mot_stg_wrong"})
        assert api.get("/v1/auth/session", headers=headers).status_code == 200
        assert api.get("/v1/auth/session", headers=AUTH).status_code == 200

    def test_a_request_with_no_credential_is_never_throttled(self, api: TestClient) -> None:
        """It costs no lookup, so there is nothing to bound.

        This is also what keeps an MCP client's unauthenticated discovery probe answering
        401 with its RFC 9728 pointer rather than 429: that probe carries no bearer.
        """
        for _ in range(MAX_FAILURES * 2):
            assert api.get("/v1/auth/session").status_code == 401
        # And the budget it did not spend is still there for a real stale session.
        assert api.get(
            "/v1/auth/session", headers={"Authorization": "Bearer nope"}
        ).status_code == (401)

    def test_a_spoofed_forwarded_for_buys_no_fresh_budget(self, api: TestClient) -> None:
        """The bucket is the process, because no per-caller key here is unspoofable.

        The API runs behind `uvicorn --forwarded-allow-ips='*'`, so `request.client.host`
        is the left-most X-Forwarded-For entry — whatever the outermost caller typed. A
        limiter keyed on it would be a limiter with a reset button.
        """
        for index in range(MAX_FAILURES):
            response = api.get(
                "/v1/auth/session",
                headers={
                    "Authorization": f"Bearer mot_stg_guess{index}",
                    "X-Forwarded-For": f"203.0.113.{index % 256}",
                },
            )
            assert response.status_code == 401
        throttled = api.get(
            "/v1/auth/session",
            headers={"Authorization": "Bearer mot_stg_one-more", "X-Forwarded-For": "198.51.100.7"},
        )
        assert throttled.status_code == 429

    def test_a_failure_every_window_never_accumulates(self) -> None:
        """The arithmetic, on a clock this test owns.

        `window_seconds=0` would make the reset branch fire unconditionally and would pass
        whether the reset worked or not, which is why `FailureThrottle` takes a clock.
        """
        now = [1000.0]
        throttle = FailureThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
        for _ in range(500):
            now[0] += 61
            assert throttle.record_failure() is False

    def test_failures_inside_one_window_do_accumulate(self) -> None:
        now = [1000.0]
        throttle = FailureThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
        for _ in range(2):
            now[0] += 1
            assert throttle.record_failure() is False
        now[0] += 1
        assert throttle.record_failure() is True
        # And the next window starts clean.
        now[0] += 60
        assert throttle.record_failure() is False

    def test_the_budget_is_spent_then_refused_then_reset(self) -> None:
        throttle = FailureThrottle(max_failures=2, window_seconds=3600)
        assert throttle.record_failure() is False
        assert throttle.record_failure() is False
        assert throttle.record_failure() is True
        throttle.reset()
        assert throttle.record_failure() is False


class TestTheCredentialNeverReachesAnErrorReport:
    def test_the_auth_path_names_its_credential_something_sentry_redacts(self) -> None:
        """``sentry_sdk`` scrubs frame locals by name; these are the names it scrubs.

        Pinned against the installed SDK rather than a copy of its list, because the point
        is that the *real* scrubber catches the *real* variable names. A rename on either
        side is a red test rather than a bearer token in GlitchTip.
        """
        from motet_api.deps import _caller_for_api_token, require_caller
        from sentry_sdk.scrubber import DEFAULT_DENYLIST

        assert "token" in DEFAULT_DENYLIST
        assert "secret" in DEFAULT_DENYLIST

        # Every local, not only the arguments: what sentry captures is the frame, and the
        # bearer spends most of its life in `require_caller` as a local rather than a
        # parameter.
        for function in (require_caller, _caller_for_api_token, token_repo.token_for_secret):
            names = set(function.__code__.co_varnames)
            credentials = names & {"token", "secret", "presented", "bearer", "plaintext"}
            assert credentials, f"{function.__qualname__} takes no credential-shaped argument"
            assert credentials <= set(DEFAULT_DENYLIST), (
                f"{function.__qualname__} names a credential "
                f"{credentials - set(DEFAULT_DENYLIST)}, which sentry_sdk's scrubber "
                "would not redact out of a frame local"
            )

    def test_the_response_model_is_not_in_its_own_repr_either(self) -> None:
        """`MintedToken` is the dataclass; this is the shape that crosses the wire.

        Pydantic's default repr prints every field, and this object is a frame local of
        the route that returns it — so without `Field(repr=False)` an unhandled exception
        during serialization ships a live credential to GlitchTip.
        """
        from motet_api.schemas import ApiTokenResponse, CreatedApiTokenResponse

        row = ApiTokenResponse(
            id="pat_1",
            prefix="mot_stg_abcd1234",
            label="x",
            email=FAKE_EMAIL,
            created_at=datetime.now(UTC),
            last_used_at=None,
            expires_at=None,
            revoked_at=None,
        )
        response = CreatedApiTokenResponse(token="mot_stg_the-actual-secret", created=row)
        assert "the-actual-secret" not in repr(response)
        # …and it is still *sent*, which is the whole point of the route.
        assert "the-actual-secret" in response.model_dump_json()

    def test_settings_does_not_print_its_secrets(self) -> None:
        """`Settings` is a frame local of `require_caller`, so its repr is an auth-path leak.

        The bearer is redacted by name; the *shared* token and the Cloud SQL URL are not,
        because `config` is not a name `sentry_sdk`'s scrubber knows. `field(repr=False)`
        is what keeps an unhandled exception on this path from carrying them to GlitchTip.
        """
        from motet_api.config import Settings

        printed = repr(
            Settings(
                database_url="postgresql://postgres:hunter2@db.invalid/motet",
                inference_mode="fake",
                api_token="the-shared-secret",
                public_base_url=None,
                app_base_url=None,
                feed_title="",
                feed_description="",
                feed_author="",
            )
        )
        assert "hunter2" not in printed
        assert "the-shared-secret" not in printed
        # Not blanket-hidden: the fields that are safe still print, or this proves nothing.
        assert "inference_mode='fake'" in printed

    def test_the_minted_token_is_not_in_its_own_repr(self) -> None:
        """An error reporter captures a local by its repr, and the route holds this one."""
        minted = token_repo.MintedToken(
            token=token_repo.ApiToken(
                id="pat_1",
                user_id=repo.OWNER_USER_ID,
                prefix="mot_stg_abcd1234",
                label="x",
                email=FAKE_EMAIL,
                created_at=datetime.now(UTC),
                last_used_at=None,
                expires_at=None,
                revoked_at=None,
            ),
            secret="mot_stg_the-actual-secret",
        )
        assert "the-actual-secret" not in repr(minted)

    def test_the_token_never_travels_in_a_url(self) -> None:
        """Header only. A query parameter lands in an access log and in a referrer.

        Asserted off the served contract rather than off the handlers, because the
        contract is what a client generator reads: a query parameter added here would
        produce a generated client that puts a credential in a URL.
        """
        paths = app.openapi()["paths"]
        for path, operations in paths.items():
            if not path.startswith("/v1/auth/tokens"):
                continue
            for operation in operations.values():
                locations = {p["in"] for p in operation.get("parameters", [])}
                # A path segment is an id and a header is where the bearer belongs. A
                # query parameter or a cookie is the shape that would put a credential
                # into an access log, a referrer, or a browser's storage.
                assert locations <= {"path", "header"}, (
                    f"{path} takes a parameter in {locations - {'path', 'header'}}"
                )


class TestTheEnvironmentLabel:
    def test_it_falls_back_to_the_deployment_environment(self) -> None:
        """No new variable is required: the deploy already labels its telemetry."""
        assert token_label({"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=staging"}) == (
            "staging"
        )

    def test_an_explicit_label_wins(self) -> None:
        assert (
            token_label(
                {
                    TOKEN_LABEL_ENV: "live",
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=production",
                }
            )
            == "live"
        )

    def test_an_unusable_label_falls_back_rather_than_refusing(self) -> None:
        """A deployment must not be unable to mint a token because of how it labels spans."""
        assert token_label({TOKEN_LABEL_ENV: "!!!"}) == DEFAULT_TOKEN_LABEL
        assert token_label({}) == DEFAULT_TOKEN_LABEL

    def test_a_label_is_slugified_rather_than_pasted_in(self) -> None:
        assert token_label(
            {"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=Staging (GCP)"}
        ) == ("staginggcp")

    def test_every_token_belongs_to_the_one_seeded_account(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """Still one account. If this ever fails, somebody built the user system."""
        mint(api, signed_in(api))
        rows = db.execute("SELECT DISTINCT user_id FROM api_tokens").fetchall()
        assert [row["user_id"] for row in rows] == [repo.OWNER_USER_ID]

    def test_a_label_of_only_spaces_is_refused(self, api: TestClient) -> None:
        """Stripped before it is validated, not after — otherwise it stores an empty one."""
        refused = api.post("/v1/auth/tokens", json={"label": "   "}, headers=signed_in(api))
        assert refused.status_code == 422

    def test_a_live_token_is_never_pushed_off_the_list_by_revoked_ones(
        self, db: psycopg.Connection[Any]
    ) -> None:
        """The list is the only revoke lever, so nothing live may fall off its bound.

        Written at the repository layer because the property is the ``ORDER BY``: with
        ``created_at DESC`` alone, a hundred revoked rows made after the live one would
        fill the page and leave a working credential with no surface that can revoke it.
        """
        oldest = token_repo.create_token(
            db, user_id=repo.OWNER_USER_ID, email=FAKE_EMAIL, label="live", environment="stg"
        )
        for index in range(token_repo.LIST_LIMIT + 5):
            newer = token_repo.create_token(
                db,
                user_id=repo.OWNER_USER_ID,
                email=FAKE_EMAIL,
                label=f"revoked {index}",
                environment="stg",
            )
            token_repo.revoke_token(db, user_id=repo.OWNER_USER_ID, token_id=newer.token.id)
        db.commit()

        listed = token_repo.list_tokens(db, repo.OWNER_USER_ID)
        assert len(listed) == token_repo.LIST_LIMIT
        assert listed[0].id == oldest.token.id
        assert all(row.revoked_at is not None for row in listed[1:])

    def test_the_marker_is_what_routes_a_bearer_to_the_token_table(self) -> None:
        assert token_repo.looks_like_api_token("mot_stg_abc")
        # A session token is 43 url-safe characters and never carries the marker.
        assert not token_repo.looks_like_api_token(auth_repo.new_session_token())
