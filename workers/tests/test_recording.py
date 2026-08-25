"""What the pipeline records about a run it has finished — motet#24 and motet#25.

Both issues describe the same defect: the work happened, the evidence was thrown away.
Neither is testable by asserting on a return value, because neither *has* one — what they
produce is a log line and a counter, so these tests read the log the worker actually wrote.

Against a real Postgres and the fakes, like the rest of ``workers/tests``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
import pytest
from motet_db import repo
from motet_inference.adapters import (
    ClaudeGroundingValidator,
    ClaudeIntegrator,
    ClaudeScriptGenerator,
)
from motet_inference.interfaces import Stages
from motet_inference.llm import FakeLlmClient
from motet_inference.registry import fake_stages
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste

USER = repo.OWNER_USER_ID

NEWSLETTER = (
    "Acme raises $20M Series A",
    "Acme raises $20M Series A. Acme announced the round on Tuesday, led by Northwind "
    "Ventures, bringing total funding to $31M.",
)

#: The sentence the grounding model will refuse. A claim can only reach the gate if the
#: script stage located its quote in the source, so an *unsupported* claim is one whose
#: quote is real and whose spoken text overstates it — which is invariant 3's own named
#: failure mode, and the only kind a model verdict can catch.
UNGROUNDED_TEXT = "Acme raised four hundred million dollars."
GROUNDED_TEXT = "Acme announced the round on Tuesday."
QUOTE = "Acme announced the round on Tuesday"

REFUSAL = "the span says the round was announced, not how large it was"


def canned(payload: object) -> FakeLlmClient:
    """A fake model that answers every request with one JSON document."""
    return FakeLlmClient(responses={"": json.dumps(payload)})


def _script_payload(news_item_id: str, source_item_id: str, *, fabricate: bool) -> object:
    claims: list[dict[str, str]] = [
        {"text": GROUNDED_TEXT, "quote": QUOTE, "source_item_id": source_item_id}
    ]
    if fabricate:
        claims.append({"text": UNGROUNDED_TEXT, "quote": QUOTE, "source_item_id": source_item_id})
    return {"segments": [{"news_item_id": news_item_id, "claims": claims}]}


def install_real_adapters(
    monkeypatch: pytest.MonkeyPatch,
    *,
    script_payload: object,
    grounding_payload: object,
) -> None:
    """Run the worker on the REAL stage adapters with a fake model underneath.

    This is the combination that has to be exercised: the recording happens inside the
    adapters, and the deterministic *stage* fakes call no model at all — so a run on those
    correctly costs nothing, and would let a missing call site pass. Injected at the
    ``Stages`` seam invariant 7 exists to provide; no vendor is reached either way.
    """
    import motet_workers.loop as loop

    base = fake_stages()
    stages = Stages(
        integrator=ClaudeIntegrator(
            canned({"decision": "new", "title": NEWSLETTER[0], "summary": "Acme raised money."})
        ),
        script_generator=ClaudeScriptGenerator(canned(script_payload)),
        grounding_validator=ClaudeGroundingValidator(canned(grounding_payload)),
        speech_synthesizer=base.speech_synthesizer,
    )
    monkeypatch.setattr(loop, "get_stages", lambda: stages)


def _assembled_episode(conn: psycopg.Connection[Any], url: str) -> tuple[str, str, str]:
    """Paste one newsletter, integrate it, and assemble an episode from it."""
    stored = enqueue_paste(conn, user_id=USER, title=NEWSLETTER[0], text=NEWSLETTER[1])
    conn.commit()
    drain(Queue.INTEGRATE, url)
    (news_item,) = repo.list_news_items(conn, USER)
    episode_id = enqueue_episode(
        conn, user_id=USER, title="Morning briefing", max_duration_ms=20 * 60_000
    )
    conn.commit()
    drain(Queue.ASSEMBLE, url)
    return episode_id, news_item.id, stored.id


class TestGroundingDropsAreCharacterised:
    def test_each_dropped_claim_is_reported_with_its_reason_and_its_text(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """motet#24: the count was recorded and the claims behind it were not.

        There is no recovering them afterwards — the pre-grounding script is never stored,
        a dropped claim leaves no row anywhere, and re-running the stage produces a
        different script. This log line is the only moment the detail exists.
        """
        episode_id, news_item_id, source_item_id = _assembled_episode(db, _migrated)
        install_real_adapters(
            monkeypatch,
            script_payload=_script_payload(news_item_id, source_item_id, fabricate=True),
            grounding_payload={
                "verdicts": [
                    {"index": 0, "supported": True},
                    {"index": 1, "supported": False, "reason": REFUSAL},
                ]
            },
        )

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.SCRIPT, _migrated)

        lines = [record.getMessage() for record in caplog.records]
        assert any("rejected 1 of 2 claims" in line and episode_id in line for line in lines), lines

        (detail,) = [line for line in lines if "grounding rejected a claim" in line]
        # Everything needed to answer "what kind of claim gets dropped": the episode it
        # belongs to, the story it was about, the bucket, the model's own words, and the
        # sentence that would have been spoken.
        assert episode_id in detail
        assert news_item_id in detail
        assert "(unsupported)" in detail
        assert REFUSAL in detail
        assert UNGROUNDED_TEXT in detail

        # And the gate still did its job: the fabricated claim is not in the database.
        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        spoken = [claim.text for segment in episode.segments for claim in segment.claims]
        assert spoken == [GROUNDED_TEXT]

    def test_an_episode_where_nothing_was_dropped_says_so_rather_than_saying_nothing(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A clean run has to be distinguishable from a stage that did not run.

        Silence on the happy path is what makes "no drops today" and "grounding never
        executed" the same observation — the trap AGENTS.md names about the obs stack
        generally: never infer "no errors" from "no data".
        """
        _, news_item_id, source_item_id = _assembled_episode(db, _migrated)
        install_real_adapters(
            monkeypatch,
            script_payload=_script_payload(news_item_id, source_item_id, fabricate=False),
            grounding_payload={"verdicts": [{"index": 0, "supported": True}]},
        )

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.SCRIPT, _migrated)

        lines = [record.getMessage() for record in caplog.records]
        assert any("passed grounding validation" in line for line in lines), lines
        assert not any("grounding rejected a claim" in line for line in lines), lines


class TestCostIsAttributable:
    def test_scripting_an_episode_leaves_a_cost_line_carrying_the_episode_id(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """motet#25: usage was decoded on every call and read by nobody.

        The id is the whole point. A metric cannot carry one without minting a time series
        per episode, so "what did *that* episode cost" has to be answered in a log line or
        not at all.
        """
        episode_id, news_item_id, source_item_id = _assembled_episode(db, _migrated)
        install_real_adapters(
            monkeypatch,
            script_payload=_script_payload(news_item_id, source_item_id, fabricate=False),
            grounding_payload={"verdicts": [{"index": 0, "supported": True}]},
        )

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.SCRIPT, _migrated)

        (line,) = [line for line in _messages(caplog) if "scripting cost" in line]
        assert episode_id in line
        # Both completions in one total: scripting and grounding an episode are two calls
        # and one question.
        assert "cost 2 completion(s)" in line
        # Every field, zeros included. `cache_read=0` on a stage that passes a large stable
        # prefix is the observation AGENTS.md says never to assume away.
        assert "cache_read=" in line

    def test_ingesting_a_source_item_leaves_a_cost_line_carrying_its_id(
        self,
        db: psycopg.Connection[Any],
        _migrated: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dedup is the volume stage, so per-item is the granularity that means anything."""
        install_real_adapters(
            monkeypatch,
            script_payload={"segments": []},
            grounding_payload={"verdicts": []},
        )
        stored = enqueue_paste(db, user_id=USER, title=NEWSLETTER[0], text=NEWSLETTER[1])
        db.commit()

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.INTEGRATE, _migrated)

        (line,) = [line for line in _messages(caplog) if "cost 1 completion(s)" in line]
        assert stored.id in line

    def test_a_run_on_the_stage_fakes_reports_no_cost_at_all(
        self, db: psycopg.Connection[Any], _migrated: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The fakes call no model, so there is nothing to bill — and nothing is claimed.

        Worth pinning: a cost line that appeared on a fake run would be a fabricated number
        in the one place a fabricated number is hardest to notice.
        """
        enqueue_paste(db, user_id=USER, title=NEWSLETTER[0], text=NEWSLETTER[1])
        db.commit()

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.INTEGRATE, _migrated)

        assert not [line for line in _messages(caplog) if "completion(s)" in line]

    def test_publishing_an_episode_records_what_was_sent_for_synthesis(
        self, db: psycopg.Connection[Any], _migrated: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Cartesia bills per character, and nothing counted them."""
        episode_id, _, _ = _assembled_episode(db, _migrated)
        drain(Queue.SCRIPT, _migrated)

        with caplog.at_level(logging.INFO, logger="motet.worker"):
            drain(Queue.TTS, _migrated)

        (line,) = [line for line in _messages(caplog) if "characters synthesized" in line]
        assert episode_id in line
        # The sum of what every segment handed to the adapter, which sends the string it
        # is given unchanged — so this is what would have been billed. A real number
        # rather than the placeholder zero a plumbed-but-unwired counter would leave.
        episode = repo.get_episode(db, episode_id)
        assert episode is not None
        expected = sum(len(segment.text) for segment in episode.segments)
        assert expected > 0
        assert f"{expected} characters synthesized" in line


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]
