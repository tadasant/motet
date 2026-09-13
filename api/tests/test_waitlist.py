"""`POST /v1/waitlist` and `GET /v1/admin/waitlist`: the landing page's signup list.

The public half is the only `/v1` route anybody may call, from another site, so most of this
module is what it refuses and what it will not reveal: a second submission of a known
address looks exactly like a first, a bot that fills the honeypot is told it succeeded, and
no address reaches a log line. The read half is the operator view's guard, applied to one
more route.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV
from motet_api.config import Settings
from motet_api.deps import reset_store
from motet_api.main import configure_cors
from motet_api.waitlist import MAX_BODY_BYTES, Outcome, Submission, answer, read_submission
from motet_db import auth as auth_repo
from motet_db import repo
from motet_db import waitlist as waitlist_repo
from motet_db.waitlist import normalize_email

TOKEN = "test-api-token"
ADMIN_EMAIL = "operator@motet.test"
MEMBER_EMAIL = "member@motet.test"
JOIN = "/v1/waitlist"
ADMIN_LIST = "/v1/admin/waitlist"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
AS_SCRIPT = {**FORM, "Accept": "application/json"}
#: What a browser sends when a form is posted without JavaScript.
AS_NATIVE_FORM = {**FORM, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
LANDING_ORIGIN = "https://landing.example.invalid"
APP_ORIGIN = "https://app.example.invalid"


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    # A locked deployment: the public route must not need the token that locks the rest.
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, f"{ADMIN_EMAIL},{MEMBER_EMAIL}")
    monkeypatch.setenv(ADMIN_EMAILS_ENV, ADMIN_EMAIL)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def rows(db: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    db.rollback()
    return db.execute("SELECT email, submissions FROM waitlist_signups ORDER BY id").fetchall()


def session_for(db: psycopg.Connection[Any], email: str) -> dict[str, str]:
    token = auth_repo.new_session_token()
    auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=email, token=token)
    db.commit()
    return {"Authorization": f"Bearer {token}"}


class TestJoining:
    def test_a_valid_address_is_stored_normalized_without_a_credential(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        response = api.post(
            JOIN,
            content="email=%20Ada.Lovelace%40Example.COM%20&motet_hp=",
            headers={**AS_SCRIPT, "Origin": LANDING_ORIGIN},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "joined"}
        # The landing page is another site; this is what lets its script read the answer.
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "no-store"
        assert rows(db) == [{"email": "ada.lovelace@example.com", "submissions": 1}]

    def test_a_duplicate_is_idempotent_and_indistinguishable(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        first = api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)
        again = api.post(JOIN, content="email=ADA%40example.com", headers=AS_SCRIPT)

        assert (first.status_code, first.json()) == (again.status_code, again.json())
        assert rows(db) == [{"email": "ada@example.com", "submissions": 2}]

    def test_a_form_posted_without_javascript_lands_on_a_page(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        response = api.post(JOIN, content="email=ada%40example.com", headers=AS_NATIVE_FORM)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "on the list" in response.text
        assert len(rows(db)) == 1


class TestRefusing:
    @pytest.mark.parametrize(
        "body",
        [
            "email=",
            "email=ada",
            "email=ada%40example",
            "email=ada%40%40example.com",
            "email=ada%20lovelace%40example.com",
            "email=ada%40example..com",
            "email=%40example.com",
            "email=" + "a" * 65 + "%40example.com",
            "email=" + "a" * 60 + "%40" + "b" * 200 + ".com",
            "email=a%40example.com&email=b%40example.com",
            "motet_hp=",
            "email=ada%40example.com%E2%80%8B",
            "email=%FF%40example.com",
        ],
    )
    def test_an_invalid_address_is_a_422_and_stores_nothing(
        self, api: TestClient, db: psycopg.Connection[Any], body: str
    ) -> None:
        response = api.post(JOIN, content=body, headers=AS_SCRIPT)

        assert response.status_code == 422
        assert "email address" in response.json()["detail"]
        assert response.headers["access-control-allow-origin"] == "*"
        assert rows(db) == []

    def test_a_filled_honeypot_is_told_it_succeeded_and_stores_nothing(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """A bot that learns it was caught learns to leave the field alone."""
        human = api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)
        bot = api.post(
            JOIN,
            content="email=bot%40example.com&motet_hp=https%3A%2F%2Fspam.example",
            headers=AS_SCRIPT,
        )

        assert (bot.status_code, bot.json()) == (human.status_code, human.json())
        assert [row["email"] for row in rows(db)] == ["ada@example.com"]

    def test_json_is_refused_because_it_would_be_preflighted(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        response = api.post(JOIN, json={"email": "ada@example.com"})
        assert response.status_code == 415
        assert rows(db) == []

    def test_a_body_over_the_cap_is_a_413(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        padding = "x" * MAX_BODY_BYTES
        response = api.post(
            JOIN, content=f"email=ada%40example.com&note={padding}", headers=AS_SCRIPT
        )
        assert response.status_code == 413
        assert rows(db) == []

    def test_a_streamed_body_over_the_cap_is_a_413(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """No Content-Length to refuse on, so the limit has to hold while reading."""

        def chunks() -> Iterator[bytes]:
            yield b"email=ada%40example.com&note="
            for _ in range(MAX_BODY_BYTES // 512 + 2):
                yield b"x" * 512

        response = api.post(JOIN, content=chunks(), headers=AS_SCRIPT)
        assert "content-length" not in {k.lower() for k in response.request.headers}
        assert response.status_code == 413
        assert rows(db) == []

    def test_a_store_that_fails_is_a_503_that_names_no_address(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The exception is caught rather than reported, and reported by type alone.

        Escaping, it would reach the error reporter with the address among the frame locals,
        and a constraint violation's message quotes the row it refused.
        """

        def refuse(_conn: object, email: str) -> bool:
            raise psycopg.errors.CheckViolation(f"Failing row contains ({email})")

        monkeypatch.setattr(waitlist_repo, "join", refuse)
        caplog.set_level(logging.DEBUG)

        response = api.post(JOIN, content="email=secret.person%40example.com", headers=AS_SCRIPT)

        assert response.status_code == 503
        assert response.headers["access-control-allow-origin"] == "*"
        assert "outcome=store_failed" in caplog.text
        assert "CheckViolation" in caplog.text
        assert "secret.person" not in caplog.text
        assert rows(db) == []

    def test_the_submission_repr_hides_the_address(self) -> None:
        assert "ada" not in repr(Submission(email="ada@example.com", refused=None, wants_json=True))

    def test_an_invalid_native_post_lands_on_a_page_that_says_so(self, api: TestClient) -> None:
        response = api.post(JOIN, content="email=nope", headers=AS_NATIVE_FORM)
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("text/html")
        assert "look like an email address" in response.text

    def test_no_address_reaches_a_log_line(
        self, api: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        api.post(JOIN, content="email=secret.person%40example.com", headers=AS_SCRIPT)
        api.post(JOIN, content="email=secret.person%40example.com", headers=AS_SCRIPT)
        api.post(JOIN, content="email=secret.person%40nowhere", headers=AS_SCRIPT)

        assert "outcome=joined" in caplog.text
        assert "outcome=already_listed" in caplog.text
        assert "secret.person" not in caplog.text


def test_the_apps_cors_policy_does_not_strip_the_wildcard() -> None:
    """Deployed, the API also carries the SPA's exact-origin CORS policy.

    That middleware adds its own header for the app's origin and leaves every other origin's
    response alone — which is what lets this route's ``*`` reach the landing page. Applied
    through ``configure_cors`` itself, like ``test_deploy_wiring``'s CORS tests, so a change
    to the real policy that started stripping it would fail here.
    """
    throwaway = FastAPI()
    configure_cors(
        throwaway,
        Settings(
            database_url=None,
            inference_mode="fake",
            api_token=TOKEN,
            public_base_url=None,
            app_base_url=APP_ORIGIN,
            feed_title="Motet",
            feed_description="",
            feed_author="Motet",
        ),
    )

    @throwaway.post(JOIN)
    async def _join(request: Request) -> Response:
        submission = await read_submission(request)
        return answer(submission.refused or Outcome.JOINED, wants_json=submission.wants_json)

    response = TestClient(throwaway).post(
        JOIN,
        content="email=ada%40example.com",
        headers={**AS_SCRIPT, "Origin": LANDING_ORIGIN},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


class TestTheAdminList:
    def test_an_admin_sees_every_signup_newest_first_and_paged(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        for name in ("first", "second", "third"):
            api.post(JOIN, content=f"email={name}%40example.com", headers=AS_SCRIPT)
        api.post(JOIN, content="email=first%40example.com", headers=AS_SCRIPT)
        admin = session_for(db, ADMIN_EMAIL)

        page = api.get(ADMIN_LIST, params={"limit": 2}, headers=admin)
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 3
        assert [s["email"] for s in body["signups"]] == ["third@example.com", "second@example.com"]
        assert body["next_before"] == body["signups"][-1]["id"]

        rest = api.get(
            ADMIN_LIST, params={"limit": 2, "before": body["next_before"]}, headers=admin
        ).json()
        assert [s["email"] for s in rest["signups"]] == ["first@example.com"]
        assert rest["signups"][0]["submissions"] == 2
        assert rest["signups"][0]["last_submitted_at"] >= rest["signups"][0]["created_at"]
        assert rest["next_before"] is None

    def test_a_signed_in_member_who_is_not_an_admin_is_a_403(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        api.post(JOIN, content="email=ada%40example.com", headers=AS_SCRIPT)
        refused = api.get(ADMIN_LIST, headers=session_for(db, MEMBER_EMAIL))
        assert refused.status_code == 403
        assert "signups" not in refused.json()

    def test_the_shared_api_token_is_not_an_admin(self, api: TestClient) -> None:
        refused = api.get(ADMIN_LIST, headers={"Authorization": f"Bearer {TOKEN}"})
        assert refused.status_code == 403

    def test_an_unauthenticated_caller_is_a_401(self, api: TestClient) -> None:
        assert api.get(ADMIN_LIST).status_code == 401


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("ada@example.com", "ada@example.com"),
        ("  Ada@Example.COM ", "ada@example.com"),
        ("ada+motet@mail.example.co.uk", "ada+motet@mail.example.co.uk"),
        ("o'brien@example.ie", "o'brien@example.ie"),
        ("ada@bücher.example", "ada@bücher.example"),
        ("ada", None),
        ("ada@localhost", None),
        ("ada@.example.com", None),
        ("ada@example.com.", None),
        ("ad\ta@example.com", None),
        ("ad\x00a@example.com", None),
        ("ada@example.com\u200b", None),
    ],
)
def test_normalize_email(raw: str, stored: str | None) -> None:
    assert normalize_email(raw) == stored
