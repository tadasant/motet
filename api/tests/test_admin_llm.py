"""PROTOTYPE — `GET/PUT /v1/admin/llm-config` and the `costs` block on the admin overview."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_db import repo
from motet_inference.llm import DEFAULT_MODEL, KNOWN_MODELS, LlmStage

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
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
    for stage in LlmStage:
        monkeypatch.delenv(stage.model_env, raising=False)
        monkeypatch.delenv(stage.effort_env, raising=False)
    monkeypatch.delenv("MOTET_LLM_MODEL", raising=False)
    monkeypatch.delenv("MOTET_LLM_EFFORT", raising=False)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def by_stage(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["stage"]: entry for entry in body["stages"]}


class TestGetConfig:
    def test_lists_every_stage_in_the_enum_with_the_chain(self, api: TestClient) -> None:
        body = api.get("/v1/admin/llm-config", headers=AUTH).json()
        assert [entry["stage"] for entry in body["stages"]] == [s.value for s in LlmStage]
        assert body["precedence"] == ["settings", "stage_env", "global_env", "default"]
        assert body["applies"] == "next_job"
        assert {m["slug"] for m in body["models"]} == set(KNOWN_MODELS)
        dedup = by_stage(body)["dedup"]
        assert dedup["model"] == DEFAULT_MODEL
        assert dedup["model_source"] == "default"
        assert dedup["effort"] == "low"
        assert dedup["setting_model"] is None
        assert dedup["default_model"] == DEFAULT_MODEL
        voice = by_stage(body)["voice"]
        assert voice["effort"] == "off"

    def test_reports_env_rungs_beneath_a_setting(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_LLM_MODEL", OPUS)
        monkeypatch.setenv("MOTET_LLM_MODEL_SCRIPT", HAIKU)
        monkeypatch.setenv("MOTET_LLM_EFFORT_SCRIPT", "off")
        body = api.get("/v1/admin/llm-config", headers=AUTH).json()
        script = by_stage(body)["script"]
        assert (script["model"], script["model_source"]) == (HAIKU, "stage_env")
        assert script["global_env_model"] == OPUS
        assert script["stage_env_model"] == HAIKU
        dedup = by_stage(body)["dedup"]
        assert (dedup["model"], dedup["model_source"]) == (OPUS, "global_env")

    def test_needs_the_token(self, api: TestClient) -> None:
        assert api.get("/v1/admin/llm-config").status_code == 401


class TestPutConfig:
    def test_set_then_read_then_clear(self, api: TestClient, db: psycopg.Connection[Any]) -> None:
        put = api.put(
            "/v1/admin/llm-config/dedup",
            headers=AUTH,
            json={"model": OPUS, "effort": "high"},
        )
        assert put.status_code == 200, put.text
        dedup = by_stage(put.json())["dedup"]
        assert (dedup["model"], dedup["model_source"]) == (OPUS, "settings")
        assert (dedup["effort"], dedup["effort_source"]) == ("high", "settings")
        assert dedup["setting_model"] == OPUS

        # The write is committed, so a fresh read — and a worker — sees it.
        got = by_stage(api.get("/v1/admin/llm-config", headers=AUTH).json())["dedup"]
        assert got["model"] == OPUS
        assert repo.load_settings(db, "llm.") == {
            "llm.model.dedup": OPUS,
            "llm.effort.dedup": "high",
        }

        # A field left out is untouched; null clears.
        cleared = api.put("/v1/admin/llm-config/dedup", headers=AUTH, json={"model": None})
        dedup = by_stage(cleared.json())["dedup"]
        assert (dedup["model"], dedup["model_source"]) == (DEFAULT_MODEL, "default")
        assert (dedup["effort"], dedup["effort_source"]) == ("high", "settings")
        assert repo.load_settings(db, "llm.") == {"llm.effort.dedup": "high"}

    def test_unknown_slug_is_a_400_and_writes_nothing(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        response = api.put(
            "/v1/admin/llm-config/dedup", headers=AUTH, json={"model": "anthropic/claude-typo"}
        )
        assert response.status_code == 400
        assert "catalogue" in response.json()["detail"]
        assert repo.load_settings(db, "llm.") == {}

    def test_an_effort_the_slug_does_not_take_is_a_400(self, api: TestClient) -> None:
        # Haiku has no selectable effort; dedup's default effort is `low`.
        response = api.put("/v1/admin/llm-config/dedup", headers=AUTH, json={"model": HAIKU})
        assert response.status_code == 400
        assert "no selectable effort" in response.json()["detail"]
        # With effort off it is a legal pairing.
        ok = api.put(
            "/v1/admin/llm-config/dedup", headers=AUTH, json={"model": HAIKU, "effort": "off"}
        )
        assert ok.status_code == 200, ok.text
        dedup = by_stage(ok.json())["dedup"]
        assert (dedup["model"], dedup["effort"]) == (HAIKU, "off")

    def test_unknown_stage_is_a_404(self, api: TestClient) -> None:
        assert api.put("/v1/admin/llm-config/tts", headers=AUTH, json={}).status_code == 404


class TestOverviewCosts:
    def test_empty_ledger_reports_every_stage_at_zero_and_no_since(self, api: TestClient) -> None:
        costs = api.get("/v1/admin/overview", headers=AUTH).json()["costs"]
        assert costs["since"] is None
        assert set(costs["stages"]) == {s.value for s in LlmStage}
        assert all(v["completions"] == 0 and v["usd"] == 0 for v in costs["stages"].values())
        assert costs["users"] == {}
        assert set(costs["queues"]) == {"integrate", "script"}

    def test_rows_are_priced_per_model_and_folded_three_ways(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        item = repo.insert_source_item(db, user_id=repo.OWNER_USER_ID, title="A", text="A.")
        # Sonnet 5 dedup: 1M uncached in ($2) + 100k out ($1) = $3.
        repo.insert_llm_usage(
            db,
            stage="dedup",
            model=DEFAULT_MODEL,
            input_tokens=1_000_000,
            output_tokens=100_000,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            subject=item.id,
            job_id=7,
        )
        # Opus dedup_confirm: 100k in ($0.50) + 10k out ($0.25) = $0.75.
        repo.insert_llm_usage(
            db,
            stage="dedup_confirm",
            model=OPUS,
            input_tokens=100_000,
            output_tokens=10_000,
            reasoning_tokens=5_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            subject=item.id,
            job_id=7,
        )
        # A voice turn with no user-resolvable subject: counted per stage, on no queue.
        repo.insert_llm_usage(
            db,
            stage="voice",
            model=DEFAULT_MODEL,
            input_tokens=10_000,
            output_tokens=1_000,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            subject="session-1",
            job_id=None,
        )
        db.commit()

        costs = api.get("/v1/admin/overview", headers=AUTH).json()["costs"]
        assert costs["since"] is not None
        assert costs["stages"]["dedup"]["usd"] == pytest.approx(3.0)
        assert costs["stages"]["dedup"]["completions"] == 1
        assert costs["stages"]["dedup_confirm"]["usd"] == pytest.approx(0.75)
        assert costs["stages"]["dedup_confirm"]["reasoning_tokens"] == 5_000
        assert costs["stages"]["voice"]["usd"] == pytest.approx(0.03)
        assert costs["stages"]["script"]["completions"] == 0

        # The user was resolved from the `si_` subject at insert time.
        owner = costs["users"][repo.OWNER_USER_ID]
        assert owner["completions"] == 2
        assert owner["usd"] == pytest.approx(3.75)
        assert owner["input_tokens"] == 1_100_000

        assert costs["queues"]["integrate"]["usd"] == pytest.approx(3.75)
        assert costs["queues"]["integrate"]["completions"] == 2
        assert costs["queues"]["script"]["completions"] == 0
