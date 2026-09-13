"""`/v1/admin/llm-config` and `/v1/admin/llm-spend`: which model, why, and what it cost.

motet#92. Most of this is refusals again, for `test_admin_overview.py`'s reason and one more:
a `PUT` here changes what every later job *spends*, so beside the operator check it has a
second lock — the deployment's own `MOTET_SETTINGS_WRITABLE`, off in production — and a
validation gate that is the worker's own. The spend half pins the arithmetic an operator
will read as dollars: per model, per cache TTL, and a model the catalogue cannot price
counted rather than treated as free.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.auth import ADMIN_EMAILS_ENV, ALLOWED_EMAILS_ENV
from motet_api.deps import reset_store
from motet_db import auth as auth_repo
from motet_db import llm_usage, repo
from motet_db import settings as settings_repo
from motet_inference.llm import ALLOW_UNLISTED_ENV, DEFAULT_MODEL, LlmStage

TOKEN = "test-api-token"
SHARED_TOKEN = {"Authorization": f"Bearer {TOKEN}"}
ADMIN_EMAIL = "operator@motet.test"
MEMBER_EMAIL = "member@motet.test"
CONFIG = "/v1/admin/llm-config"
SPEND = "/v1/admin/llm-spend"
HAIKU = "anthropic/claude-haiku-4.5"
OPUS = "anthropic/claude-opus-5"


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, f"{ADMIN_EMAIL},{MEMBER_EMAIL}")
    monkeypatch.setenv(ADMIN_EMAILS_ENV, ADMIN_EMAIL)
    monkeypatch.delenv(settings_repo.SETTINGS_WRITABLE_ENV, raising=False)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def session_for(db: psycopg.Connection[Any], email: str) -> dict[str, str]:
    token = auth_repo.new_session_token()
    auth_repo.create_session(db, user_id=repo.OWNER_USER_ID, email=email, token=token)
    db.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def as_admin(db: psycopg.Connection[Any]) -> dict[str, str]:
    return session_for(db, ADMIN_EMAIL)


@pytest.fixture
def writable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings_repo.SETTINGS_WRITABLE_ENV, "1")


def _rows(db: psycopg.Connection[Any]) -> dict[str, str]:
    db.rollback()  # see the API's commits, not this connection's snapshot
    return settings_repo.load(db, "llm.")


class TestOnlyAnAdminGetsIn:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", CONFIG), ("PUT", f"{CONFIG}/dedup"), ("GET", SPEND)],
    )
    def test_the_shared_token_and_a_member_are_refused(
        self, api: Any, db: psycopg.Connection[Any], writable: None, method: str, path: str
    ) -> None:
        member = session_for(db, MEMBER_EMAIL)
        for headers in (SHARED_TOKEN, member):
            response = api.request(method, path, headers=headers, json={"model": HAIKU})
            assert response.status_code == 403, response.text
        assert _rows(db) == {}


class TestReadingTheConfig:
    def test_every_stage_with_its_chain_and_the_catalogue(
        self, api: Any, as_admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_LLM_EFFORT_SCRIPT", "max")
        body = api.get(CONFIG, headers=as_admin).json()
        assert [s["stage"] for s in body["stages"]] == [stage.value for stage in LlmStage]
        assert body["precedence"] == ["settings", "stage_env", "global_env", "default"]
        assert body["applies"] == "next_job"
        assert body["writable"] is False
        assert body["writable_env"] == "MOTET_SETTINGS_WRITABLE"
        script = next(s for s in body["stages"] if s["stage"] == "script")
        assert (script["effort"], script["effort_source"]) == ("max", "stage_env")
        assert (script["model"], script["model_source"]) == (DEFAULT_MODEL, "default")
        assert script["default_effort"] == "high"
        haiku = next(m for m in body["models"] if m["slug"] == HAIKU)
        assert haiku["efforts"] == [] and haiku["cache_write_1h_usd_per_mtok"] == 2.0

    def test_where_settings_are_off_a_stored_row_is_neither_applied_nor_shown(
        self, api: Any, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        settings_repo.put(db, "llm.model.script", OPUS)
        db.commit()
        script = next(
            s for s in api.get(CONFIG, headers=as_admin).json()["stages"] if s["stage"] == "script"
        )
        assert (script["model"], script["setting_model"]) == (DEFAULT_MODEL, None)

    def test_a_stale_row_is_reported_not_a_500(
        self, api: Any, db: psycopg.Connection[Any], as_admin: dict[str, str], writable: None
    ) -> None:
        settings_repo.put(db, "llm.model.dedup", "vendor/withdrawn")
        db.commit()
        body = api.get(CONFIG, headers=as_admin).json()
        assert "not in the model catalogue" in body["settings_error"]
        dedup = next(s for s in body["stages"] if s["stage"] == "dedup")
        assert (dedup["model"], dedup["model_source"]) == (DEFAULT_MODEL, "default")


class TestWritingTheConfig:
    def test_production_refuses_to_write_anything(
        self, api: Any, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        response = api.put(f"{CONFIG}/dedup", headers=as_admin, json={"model": OPUS})
        assert response.status_code == 409
        assert "MOTET_SETTINGS_WRITABLE" in response.json()["detail"]
        assert _rows(db) == {}

    def test_set_read_back_and_clear(
        self, api: Any, db: psycopg.Connection[Any], as_admin: dict[str, str], writable: None
    ) -> None:
        response = api.put(
            f"{CONFIG}/dedup", headers=as_admin, json={"model": HAIKU, "effort": "off"}
        )
        assert response.status_code == 200, response.text
        dedup = next(s for s in response.json()["stages"] if s["stage"] == "dedup")
        assert (dedup["model"], dedup["model_source"]) == (HAIKU, "settings")
        assert (dedup["effort"], dedup["effort_source"]) == ("off", "settings")
        assert _rows(db) == {"llm.effort.dedup": "off", "llm.model.dedup": HAIKU}

        # A key left out is untouched; null clears.
        api.put(f"{CONFIG}/dedup", headers=as_admin, json={"model": OPUS})
        assert _rows(db) == {"llm.effort.dedup": "off", "llm.model.dedup": OPUS}
        api.put(f"{CONFIG}/dedup", headers=as_admin, json={"model": None, "effort": None})
        assert _rows(db) == {}
        dedup = next(
            s for s in api.get(CONFIG, headers=as_admin).json()["stages"] if s["stage"] == "dedup"
        )
        assert dedup["model_source"] == "default"

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"model": "anthropic/claude-sonnet-9"}, "not in the model catalogue"),
            ({"model": HAIKU}, "no selectable effort"),  # dedup's default effort is `low`
            ({"effort": "turbo"}, "is not one of"),
        ],
    )
    def test_a_change_that_does_not_resolve_is_a_400_and_writes_nothing(
        self,
        api: Any,
        db: psycopg.Connection[Any],
        as_admin: dict[str, str],
        writable: None,
        body: dict[str, str],
        message: str,
    ) -> None:
        response = api.put(f"{CONFIG}/dedup", headers=as_admin, json=body)
        assert response.status_code == 400
        assert message in response.json()["detail"]
        assert _rows(db) == {}

    def test_a_row_is_never_the_way_around_the_catalogue(
        self,
        api: Any,
        db: psycopg.Connection[Any],
        as_admin: dict[str, str],
        writable: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ALLOW_UNLISTED_ENV, "true")
        response = api.put(f"{CONFIG}/script", headers=as_admin, json={"model": "vendor/new"})
        assert response.status_code == 400
        assert _rows(db) == {}

    def test_an_unknown_stage_is_a_404_and_an_unknown_field_a_422(
        self, api: Any, as_admin: dict[str, str], writable: None
    ) -> None:
        assert api.put(f"{CONFIG}/grounding", headers=as_admin, json={}).status_code == 404
        response = api.put(f"{CONFIG}/dedup", headers=as_admin, json={"modle": OPUS})
        assert response.status_code == 422


def _ledger(
    db: psycopg.Connection[Any],
    rows: list[tuple[str, str, int, int, int, str | None]],
    *,
    user_id: str | None = repo.OWNER_USER_ID,
    age: timedelta | None = None,
) -> None:
    """Ledger rows as ``(stage, model, input, output, cache_write, ttl)``."""
    before = db.execute("SELECT coalesce(max(id), 0) AS id FROM llm_usage").fetchone()
    assert before is not None
    with db.transaction():
        llm_usage.insert(
            db,
            [
                llm_usage.UsageRow(stage, model, inp, out, 0, 0, write, ttl)
                for stage, model, inp, out, write, ttl in rows
            ],
            subject=None,
            job_id=None,
        )
        # No subject to resolve a user from, so set the one the case is about directly.
        db.execute(
            "UPDATE llm_usage SET user_id = %s, occurred_at = now() - %s WHERE id > %s",
            (user_id, age or timedelta(0), before["id"]),
        )
    db.commit()


class TestSpend:
    def test_an_empty_ledger_is_every_stage_at_zero(
        self, api: Any, as_admin: dict[str, str]
    ) -> None:
        body = api.get(SPEND, headers=as_admin).json()
        assert body["since"] is None
        assert body["window_days"] == 7 and body["retention_days"] == 90
        assert set(body["total"]["stages"]) == {stage.value for stage in LlmStage}
        assert set(body["total"]["queues"]) == {"integrate", "script"}
        assert all(spend["usd"] == 0 for spend in body["total"]["stages"].values())
        assert body["total"]["users"] == []

    def test_folded_per_stage_user_queue_and_window_at_the_billed_rates(
        self, api: Any, db: psycopg.Connection[Any], as_admin: dict[str, str]
    ) -> None:
        # Sonnet 5, 1M input of which 1M written at the 1h TTL ($4), and 100k output ($1).
        _ledger(db, [("dedup", DEFAULT_MODEL, 1_000_000, 100_000, 1_000_000, "1h")])
        # The same on the script stage, written at 5m ($2.50), eight days ago.
        _ledger(
            db,
            [("script", DEFAULT_MODEL, 1_000_000, 100_000, 1_000_000, "5m")],
            age=timedelta(days=8),
        )
        # Reported under its dated snapshot, as OpenRouter does: 1M uncached input on
        # Sonnet 4.6 ($3).
        _ledger(
            db, [("dedup_confirm", "anthropic/claude-4.6-sonnet-20260217", 1_000_000, 0, 0, None)]
        )
        # A model the catalogue cannot price.
        _ledger(db, [("dedup", "vendor/unlisted", 500, 50, 0, None)])

        body = api.get(SPEND, headers=as_admin).json()
        total, window = body["total"], body["window"]
        assert total["stages"]["dedup"]["usd"] == pytest.approx(5.0)
        assert total["stages"]["dedup"]["completions"] == 2
        assert total["stages"]["dedup"]["unpriced_completions"] == 1
        assert total["stages"]["script"]["usd"] == pytest.approx(3.5)
        assert total["stages"]["dedup_confirm"]["usd"] == pytest.approx(3.0)
        assert total["stages"]["voice"]["completions"] == 0
        assert total["queues"]["integrate"]["usd"] == pytest.approx(8.0)
        assert total["queues"]["script"]["usd"] == pytest.approx(3.5)
        ((user,),) = [total["users"]]
        assert user["user_id"] == repo.OWNER_USER_ID and user["spend"]["usd"] == pytest.approx(11.5)

        # The eight-day-old script row is in the total and not in the week.
        assert window["stages"]["script"]["usd"] == 0
        assert window["queues"]["integrate"]["usd"] == pytest.approx(8.0)


class TestHealth:
    def test_production_reports_no_override_without_asking_the_database(
        self, api: Any, db: psycopg.Connection[Any]
    ) -> None:
        settings_repo.put(db, "llm.model.script", OPUS)
        db.commit()
        body = api.get("/internal/health").json()
        assert body["settings_writable"] is False
        assert body["llm_overrides_in_force"] is False

    def test_staging_reports_an_override_in_force(
        self, api: Any, db: psycopg.Connection[Any], writable: None
    ) -> None:
        assert api.get("/internal/health").json()["llm_overrides_in_force"] is False
        settings_repo.put(db, "llm.model.script", OPUS)
        db.commit()
        body = api.get("/internal/health").json()
        assert body["settings_writable"] is True
        assert body["llm_overrides_in_force"] is True
