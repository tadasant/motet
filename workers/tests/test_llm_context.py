"""The worker reads ``settings`` per job — only where it may — and ledgers every completion.

motet#92. Driven through :func:`~motet_workers.drain` with a *real* dedup adapter over the
LLM fake, because the deterministic stage fakes call no model and so, correctly, leave no
row. What is pinned is what the admin screen and the production interlock rest on:

* where ``MOTET_SETTINGS_WRITABLE`` is on, a row the API wrote changes the model the very
  next job asks for, and does not outlive the job;
* where it is off — production — a row changes nothing, however it got into the table;
* a row that does not resolve costs an ERROR, never a job;
* every completion lands as one ledger row, with the job, the user and the TTL on it —
  including one billed inside a job that then failed and rolled back.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from motet_db import llm_usage, repo
from motet_db import settings as settings_repo
from motet_inference.adapters import ClaudeIntegrator
from motet_inference.llm import DEFAULT_MODEL, FakeLlmClient, LlmStage, load_config
from motet_inference.registry import fake_stages
from motet_workers import Queue, drain, enqueue_paste
from motet_workers.loop import prune_jobs

OPUS = "anthropic/claude-opus-5"
HAIKU = "anthropic/claude-haiku-4.5"

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


def _paste(db: psycopg.Connection[Any], text: str = "Acme raises $20M Series A.") -> str:
    item = enqueue_paste(db, user_id=repo.OWNER_USER_ID, title="Acme raises", text=text)
    db.commit()
    return item.id


def _override(db: psycopg.Connection[Any], rows: dict[str, str]) -> None:
    """What ``PUT /v1/admin/llm-config/dedup`` writes, without the API in the loop."""
    for key, value in rows.items():
        settings_repo.put(db, key, value)
    db.commit()


@pytest.fixture
def writable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings_repo.SETTINGS_WRITABLE_ENV, "1")


class TestLedger:
    def test_one_row_per_completion_with_job_user_subject_and_ttl(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        item_id = _paste(db)
        assert (
            drain(
                Queue.INTEGRATE, _migrated, stages=_stages(FakeLlmClient(responses={"": UNRELATED}))
            )
            == 1
        )

        rows = db.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()
        assert len(rows) == 1
        (row,) = rows
        assert (row["stage"], row["model"], row["cache_ttl"]) == ("dedup", DEFAULT_MODEL, "1h")
        assert row["input_tokens"] > 0 and row["output_tokens"] > 0
        assert (row["user_id"], row["subject"]) == (repo.OWNER_USER_ID, item_id)
        job = db.execute("SELECT id FROM jobs WHERE queue = 'integrate'").fetchone()
        assert job is not None and row["job_id"] == job["id"]

    def test_the_fake_stages_leave_no_row(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=fake_stages()) == 1
        assert db.execute("SELECT count(*) AS n FROM llm_usage").fetchone() == {"n": 0}

    def test_a_completion_billed_inside_a_job_that_rolled_back_is_still_recorded(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """The model answered, was billed, and the answer could not be used — so the
        handler raised and its transaction rolled back. The bill did not."""
        _paste(db)
        drain(Queue.INTEGRATE, _migrated, stages=_stages(FakeLlmClient(responses={"": "not json"})))
        job = db.execute("SELECT state, attempts FROM jobs WHERE queue = 'integrate'").fetchone()
        assert job is not None and job["state"] == "ready" and job["attempts"] == 1
        assert db.execute("SELECT count(*) AS n FROM llm_usage").fetchone() == {"n": 1}


class TestSettingsPerJob:
    def test_a_row_changes_the_next_job_without_a_restart(
        self, db: psycopg.Connection[Any], _migrated: str, writable: None
    ) -> None:
        client = FakeLlmClient(responses={"": UNRELATED})
        stages = _stages(client)

        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=stages) == 1
        assert client.calls[-1].model == DEFAULT_MODEL

        _override(db, {"llm.model.dedup": HAIKU, "llm.effort.dedup": "off"})
        _paste(db, "Globex opens a plant.")
        assert drain(Queue.INTEGRATE, _migrated, stages=stages) == 1
        assert client.calls[-1].model == HAIKU
        assert client.calls[-1].reasoning is None

        models = [r["model"] for r in db.execute("SELECT model FROM llm_usage ORDER BY id")]
        assert models == [DEFAULT_MODEL, HAIKU]
        # And nothing leaked back into this context once the job was over.
        assert load_config().for_stage(LlmStage.DEDUP).model == DEFAULT_MODEL

    def test_production_ignores_a_row_however_it_got_there(
        self, db: psycopg.Connection[Any], _migrated: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(settings_repo.SETTINGS_WRITABLE_ENV, raising=False)
        _override(db, {"llm.model.dedup": OPUS})
        client = FakeLlmClient(responses={"": UNRELATED})
        _paste(db)
        assert drain(Queue.INTEGRATE, _migrated, stages=_stages(client)) == 1
        assert client.calls[-1].model == DEFAULT_MODEL

    def test_a_row_that_does_not_resolve_costs_an_error_not_the_job(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        writable: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Haiku takes no effort and dedup's default is `low`, so this pairing cannot
        # resolve — the shape a slug removed from the catalogue by a later deploy has too.
        _override(db, {"llm.model.dedup": HAIKU})
        client = FakeLlmClient(responses={"": UNRELATED})
        _paste(db)
        with caplog.at_level(logging.ERROR, logger="motet.worker.llm"):
            assert drain(Queue.INTEGRATE, _migrated, stages=_stages(client)) == 1
        assert client.calls[-1].model == DEFAULT_MODEL
        job = db.execute("SELECT state FROM jobs WHERE queue = 'integrate'").fetchone()
        assert job == {"state": "done"}
        assert "do not resolve" in caplog.text


class TestRetention:
    def test_the_sweep_deletes_only_rows_past_the_window(
        self, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        row = llm_usage.UsageRow("dedup", DEFAULT_MODEL, 10, 5, 0, 0, 0, None)
        with db.transaction():
            llm_usage.insert(db, [row, row], subject=None, job_id=None)
        old = timedelta(seconds=llm_usage.RETENTION_SECONDS + 3600)
        db.execute(
            "UPDATE llm_usage SET occurred_at = now() - %s "
            "WHERE id = (SELECT min(id) FROM llm_usage)",
            (old,),
        )
        db.commit()

        prune_jobs(_migrated)

        assert db.execute("SELECT count(*) AS n FROM llm_usage").fetchone() == {"n": 1}

    def test_prune_refuses_a_connection_inside_a_transaction(
        self, db: psycopg.Connection[Any]
    ) -> None:
        with pytest.raises(ValueError, match="autocommit"):
            llm_usage.prune(db)


class TestTheSwitch:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " 1 "])
    def test_true_values(self, raw: str) -> None:
        assert settings_repo.settings_writable({settings_repo.SETTINGS_WRITABLE_ENV: raw})

    @pytest.mark.parametrize("raw", ["", "0", "false", "on", "enabled"])
    def test_everything_else_is_off(self, raw: str) -> None:
        assert not settings_repo.settings_writable({settings_repo.SETTINGS_WRITABLE_ENV: raw})

    def test_unset_is_off(self) -> None:
        assert not settings_repo.settings_writable({})
