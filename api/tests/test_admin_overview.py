"""`GET /v1/admin/overview`: the whole deployment at a glance, across every user.

Two halves, and the first is the one that matters. **This is the first route family that
returns data across users** — every address, every count, every job's `last_error` — so
most of this module is the refusals: who may *not* read it, proved one caller at a time,
and a walk of the app's routes proving nothing under `/v1/admin` escaped the guard. The
second half is what an admin sees once let in.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV, admin_emails
from motet_api.deps import require_admin, reset_store
from motet_db import auth as auth_repo
from motet_db import repo
from motet_workers import Queue, jobs
from starlette.routing import Route

TOKEN = "test-api-token"
SHARED_TOKEN = {"Authorization": f"Bearer {TOKEN}"}
ADMIN_EMAIL = "operator@motet.test"
MEMBER_EMAIL = "member@motet.test"
OVERVIEW = "/v1/admin/overview"


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    # Both addresses may sign in; only one of them is an operator.
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, f"{ADMIN_EMAIL},{MEMBER_EMAIL}")
    monkeypatch.setenv(ADMIN_EMAILS_ENV, ADMIN_EMAIL)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def session_for(db: psycopg.Connection[Any], email: str) -> dict[str, str]:
    """A signed-in browser's bearer header for ``email``.

    Written straight into `auth_sessions` rather than walked through the Google flow,
    because the flow is `test_auth.py`'s subject and this module's is what a session may
    read once it exists — including sessions for addresses the fake provider cannot mint.
    """
    token = auth_repo.new_session_token()
    auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=email, token=token)
    db.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def as_admin(db: psycopg.Connection[Any]) -> dict[str, str]:
    return session_for(db, ADMIN_EMAIL)


class TestOnlyAnAdminGetsIn:
    """Every caller that is not a listed, signed-in person is refused — server-side."""

    def test_an_admin_session_is_let_in(self, api: TestClient, as_admin: dict[str, str]) -> None:
        assert api.get(OVERVIEW, headers=as_admin).status_code == 200

    def test_an_unauthenticated_caller_is_a_401(self, api: TestClient) -> None:
        assert api.get(OVERVIEW).status_code == 401
        assert api.get(OVERVIEW, headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_a_signed_in_user_who_is_not_an_admin_is_a_403(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """The case the prototype got wrong: signed in, allowed to use Motet, not an admin.

        A 403 and not a 401, because a 401 tells the SPA its session is dead and it would
        sign a perfectly good user out.
        """
        member = session_for(db, MEMBER_EMAIL)
        assert api.get("/v1/news-items", headers=member).status_code == 200

        refused = api.get(OVERVIEW, headers=member)
        assert refused.status_code == 403
        assert ADMIN_EMAILS_ENV in refused.json()["detail"]
        assert "users" not in refused.json()

    def test_an_unset_admin_list_means_nobody(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fails closed: the address that *would* be an admin is refused once the list goes."""
        headers = session_for(db, ADMIN_EMAIL)
        monkeypatch.delenv(ADMIN_EMAILS_ENV)

        refused = api.get(OVERVIEW, headers=headers)
        assert refused.status_code == 403
        assert "unset" in refused.json()["detail"]

    @pytest.mark.parametrize("value", ["", "   ", " , ,, "])
    def test_an_empty_admin_list_means_nobody(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        """Empty is unset: a Cloud Run service definition cannot tell the two apart."""
        headers = session_for(db, ADMIN_EMAIL)
        monkeypatch.setenv(ADMIN_EMAILS_ENV, value)
        assert api.get(OVERVIEW, headers=headers).status_code == 403

    def test_the_shared_api_token_is_never_an_admin(self, api: TestClient) -> None:
        """It belongs to no person, so there is no address to find on the list.

        If it passed, "unset means nobody is an admin" would not be literally true: every
        script and every device holding the shared secret would read every user's data.
        """
        refused = api.get(OVERVIEW, headers=SHARED_TOKEN)
        assert refused.status_code == 403
        assert "shared API token" in refused.json()["detail"]

    def test_an_open_deployment_is_not_an_admin(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No MOTET_API_TOKEN means no lock on /v1 — and still no key to /v1/admin."""
        monkeypatch.delenv("MOTET_API_TOKEN")
        assert api.get("/v1/news-items").status_code == 200
        assert api.get(OVERVIEW).status_code == 403

    def test_the_admin_list_is_a_narrowing_of_the_sign_in_list_not_a_second_door(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An admin who leaves MOTET_ALLOWED_EMAILS is signed out, admin list or not."""
        headers = session_for(db, ADMIN_EMAIL)
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, MEMBER_EMAIL)
        assert api.get(OVERVIEW, headers=headers).status_code == 401

    def test_addresses_match_the_way_the_sign_in_list_matches(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ADMIN_EMAILS_ENV, f" someone@else.test , {ADMIN_EMAIL.upper()} ")
        assert api.get(OVERVIEW, headers=session_for(db, ADMIN_EMAIL)).status_code == 200


class TestTheSessionSaysWhetherToOfferTheScreen:
    """`/v1/auth/session`'s `admin` is the guard's own predicate, so the two cannot differ."""

    def test_true_for_an_admin_and_false_for_everybody_else(
        self,
        api: TestClient,
        db: psycopg.Connection[Any],
        as_admin: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        member = session_for(db, MEMBER_EMAIL)
        assert api.get("/v1/auth/session", headers=as_admin).json()["admin"] is True
        assert api.get("/v1/auth/session", headers=member).json()["admin"] is False
        assert api.get("/v1/auth/session", headers=SHARED_TOKEN).json()["admin"] is False

        monkeypatch.delenv(ADMIN_EMAILS_ENV)
        assert api.get("/v1/auth/session", headers=as_admin).json()["admin"] is False


def test_every_admin_route_is_behind_the_admin_check() -> None:
    """A route added under /v1/admin without the router's guard is a quiet disclosure.

    Walks the real app rather than trusting each declaration: a route that forgot `Admin`
    would type-check, serve, and pass every other test.

    **The walk can only see what `app.routes` lists**, and this FastAPI mounts an included
    `APIRouter` as one opaque entry rather than copying its routes out. So the first
    assertion is that nothing is hiding: every route is an `APIRoute` or one of
    Starlette's own docs routes. An included router fails it loudly, which is the point —
    whoever adds one has to teach this walk (and `test_reserved_paths.py`'s) to look inside.
    """
    opaque = [
        type(route).__name__
        for route in app.routes
        if not isinstance(route, APIRoute) and type(route) is not Route
    ]
    assert opaque == [], "a route this walk cannot look inside — see the docstring"

    def depends_on_admin(route: APIRoute) -> bool:
        pending = list(route.dependant.dependencies)
        while pending:
            dependency = pending.pop()
            if dependency.call is require_admin:
                return True
            pending.extend(dependency.dependencies)
        return False

    admin_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/admin")
    ]
    assert admin_routes, "no /v1/admin routes found"
    unguarded = [route.path for route in admin_routes if not depends_on_admin(route)]
    assert unguarded == []


def test_the_admin_list_parses_the_way_the_sign_in_list_does() -> None:
    assert admin_emails({}) == frozenset()
    assert admin_emails({ADMIN_EMAILS_ENV: " A@X.test ,, b@y.test "}) == {"a@x.test", "b@y.test"}


class TestAdminOverview:
    def test_reports_users_queues_and_resolved_jobs(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        item = repo.insert_source_item(
            db, user_id=repo.OWNER_USER_ID, title="Acme raises", text="Acme raises $20M."
        )
        job_id = jobs.enqueue(
            db, Queue.INTEGRATE, {"source_item_id": item.id}, serialize_key=repo.OWNER_USER_ID
        )
        repo.record_worker_heartbeat(db, Queue.INTEGRATE.value)
        db.commit()

        body = api.get(OVERVIEW, headers=as_admin).json()
        assert body["generated_at"]

        # Every user, every counter, zeros included.
        assert [user["user_id"] for user in body["users"]] == [repo.OWNER_USER_ID]
        owner = body["users"][0]
        assert owner["email"] is None
        assert owner["source_items"] == {"pending": 1, "integrated": 0, "failed": 0}
        assert owner["news_items"] == {"unread": 0, "read": 0}
        assert owner["episodes"] == {
            "pending": 0,
            "scripting": 0,
            "rendering": 0,
            "ready": 0,
            "failed": 0,
        }
        assert owner["jobs"] == {"ready": 1, "running": 0, "done": 0, "failed": 0}

        # Every pipeline queue, in order, even the empty ones.
        assert [queue["queue"] for queue in body["queues"]] == [
            "poll",
            "extract",
            "integrate",
            "assemble",
            "script",
            "tts",
        ]
        by_queue = {queue["queue"]: queue for queue in body["queues"]}
        integrate = by_queue["integrate"]
        counts = tuple(integrate[state] for state in ("ready", "running", "done", "failed"))
        assert counts == (1, 0, 0, 0)
        assert integrate["oldest_ready_age_s"] is not None and integrate["oldest_ready_age_s"] >= 0
        assert integrate["last_heartbeat_at"] is not None
        assert by_queue["tts"]["oldest_ready_age_s"] is None
        assert by_queue["tts"]["last_heartbeat_at"] is None

        # The job is resolved to its user and its subject through the payload.
        assert len(body["jobs"]) == 1
        assert body["jobs_next_before"] is None
        job = body["jobs"][0]
        assert job["id"] == job_id
        assert (job["queue"], job["state"], job["attempts"]) == ("integrate", "ready", 0)
        assert job["user_id"] == repo.OWNER_USER_ID
        assert job["subject"] == item.id
        assert job["last_error"] is None
        assert job["locked_at"] is None
        assert job["run_at"] and job["created_at"] and job["updated_at"]

    def test_user_filter_narrows_jobs_but_not_aggregates(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        jobs.enqueue(db, Queue.TTS, {"episode_id": "ep_missing"})
        db.commit()

        everyone = api.get(OVERVIEW, headers=as_admin).json()
        assert [job["user_id"] for job in everyone["jobs"]] == [None]
        assert everyone["jobs"][0]["subject"] == "ep_missing"

        filtered = api.get(OVERVIEW, params={"user_id": "nobody"}, headers=as_admin).json()
        assert filtered["jobs"] == []
        # Aggregates are still for everyone (ages tick between the two calls, so compare counts).
        assert [q["ready"] for q in filtered["queues"]] == [q["ready"] for q in everyone["queues"]]
        assert filtered["users"] == everyone["users"]


class TestTheJobListIsBounded:
    """A page, newest first, and every older job reachable by following the cursor."""

    def enqueue(self, db: psycopg.Connection[Any], count: int) -> list[int]:
        ids = [jobs.enqueue(db, Queue.TTS, {"episode_id": f"ep_{n}"}) for n in range(count)]
        db.commit()
        return ids

    def test_following_the_cursor_visits_every_job_exactly_once(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        ids = self.enqueue(db, 5)

        seen: list[int] = []
        params: dict[str, int] = {"limit": 2}
        pages = 0
        while True:
            page = api.get(OVERVIEW, params=params, headers=as_admin).json()
            assert len(page["jobs"]) <= 2
            seen.extend(job["id"] for job in page["jobs"])
            pages += 1
            if page["jobs_next_before"] is None:
                break
            params = {"limit": 2, "before": page["jobs_next_before"]}

        assert seen == sorted(ids, reverse=True)
        assert pages == 3

    def test_an_exactly_full_last_page_says_there_is_nothing_after_it(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        """The page is fetched one row long, so "full" is not mistaken for "more"."""
        self.enqueue(db, 2)
        page = api.get(OVERVIEW, params={"limit": 2}, headers=as_admin).json()
        assert len(page["jobs"]) == 2
        assert page["jobs_next_before"] is None

    def test_a_job_enqueued_between_pages_does_not_shift_the_next_one(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        """Why a keyset and not an offset: workers insert at the head while an admin reads."""
        ids = self.enqueue(db, 3)
        first = api.get(OVERVIEW, params={"limit": 2}, headers=as_admin).json()
        self.enqueue(db, 1)
        second = api.get(
            OVERVIEW, params={"limit": 2, "before": first["jobs_next_before"]}, headers=as_admin
        ).json()
        assert [job["id"] for job in second["jobs"]] == [ids[0]]

    def test_the_default_page_is_bounded(
        self, api: TestClient, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        from motet_api.main import ADMIN_JOBS_DEFAULT_LIMIT

        self.enqueue(db, ADMIN_JOBS_DEFAULT_LIMIT + 1)
        page = api.get(OVERVIEW, headers=as_admin).json()
        assert len(page["jobs"]) == ADMIN_JOBS_DEFAULT_LIMIT
        assert page["jobs_next_before"] == page["jobs"][-1]["id"]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"before": 0}])
    def test_a_page_outside_the_bound_is_refused(
        self, api: TestClient, as_admin: dict[str, str], params: dict[str, int]
    ) -> None:
        assert api.get(OVERVIEW, params=params, headers=as_admin).status_code == 422
