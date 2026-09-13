"""PROTOTYPE — the worker reads `settings` per job and writes `llm_usage` per completion.

Driven through :func:`~motet_workers.drain` with a *real* dedup adapter over the LLM fake,
because the deterministic stage fakes call no model and so — correctly — leave no row. What
is pinned is the pair of properties the admin screen rests on: a settings row written by
the API changes the model the very next job asks for, and every completion a job makes
lands as one ledger row with the job's id and user on it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import psycopg
from motet_db import repo
from motet_inference.adapters import ClaudeIntegrator
from motet_inference.llm import DEFAULT_MODEL, FakeLlmClient
from motet_inference.registry import fake_stages
from motet_workers import Queue, drain, enqueue_paste

OPUS = "anthropic/claude-opus-5"

UNRELATED = json.dumps(
    {
        "closest_news_item_id": None,
        "relation": "unrelated",
        "reason": "Nothing like it in the backlog.",
        "title": "Acme raises $20M",
        "summary": "Acme raised a Series A.",
    }
)


def _stages(client: FakeLlmClient) -> Any:
    return replace(fake_stages(), integrator=ClaudeIntegrator(client))


def _paste(db: psycopg.Connection[Any]) -> str:
    item = enqueue_paste(
        db, user_id=repo.OWNER_USER_ID, title="Acme raises", text="Acme raises $20M Series A."
    )
    db.commit()
    return item.id


class TestLedger:
    def test_one_row_per_completion_with_job_and_user(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        item_id = _paste(db)
        client = FakeLlmClient(responses={"": UNRELATED})

        assert drain(Queue.INTEGRATE, _migrated, stages=_stages(client)) == 1

        rows = db.execute(
            "SELECT stage, model, input_tokens, output_tokens, user_id, subject, job_id "
            "FROM llm_usage ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        (row,) = rows
        assert row["stage"] == "dedup"
        assert row["model"] == DEFAULT_MODEL
        assert row["input_tokens"] > 0 and row["output_tokens"] > 0
        assert row["user_id"] == repo.OWNER_USER_ID
        assert row["subject"] == item_id
        job = db.execute("SELECT id FROM jobs WHERE queue = 'integrate'").fetchone()
        assert job is not None and row["job_id"] == job["id"]

    def test_the_fake_stages_leave_no_row(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=fake_stages()) == 1
        count = db.execute("SELECT count(*) AS n FROM llm_usage").fetchone()
        assert count is not None and count["n"] == 0


class TestSettingsPerJob:
    def test_a_settings_row_changes_the_next_job_without_a_restart(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        client = FakeLlmClient(responses={"": UNRELATED})
        stages = _stages(client)

        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=stages) == 1
        assert client.calls[-1].model == DEFAULT_MODEL

        # What `PUT /v1/admin/llm-config/dedup` writes, without the API in the loop.
        repo.put_setting(db, "llm.model.dedup", OPUS)
        repo.put_setting(db, "llm.effort.dedup", "off")
        db.commit()

        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=stages) == 1
        assert client.calls[-1].model == OPUS
        assert client.calls[-1].reasoning is None

        # And the ledger row names the model the call actually went out on.
        models = [
            r["model"] for r in db.execute("SELECT model FROM llm_usage ORDER BY id").fetchall()
        ]
        assert models == [DEFAULT_MODEL, OPUS]

    def test_the_override_does_not_leak_past_the_job(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        from motet_inference.llm import LlmStage, load_config

        repo.put_setting(db, "llm.model.dedup", OPUS)
        repo.put_setting(db, "llm.effort.dedup", "off")
        db.commit()
        _paste(db)
        drain(Queue.INTEGRATE, _migrated, stages=_stages(FakeLlmClient(responses={"": UNRELATED})))
        # Back in the test's own context, nothing is installed.
        assert load_config().for_stage(LlmStage.DEDUP).model == DEFAULT_MODEL
