"""A whole morning's backlog through the narration path — motet#42.

Nineteen news items is what a normal Tuesday looks like, and it is the size at which
grounding validation stopped working: one batched call carrying every claim, a fixed 8k
ceiling, every token of it spent reasoning, no verdicts at all, and an episode that sat in
``scripting`` through every retry because the next attempt did exactly the same thing.

The stage adapters here are the **real** ones with a budget-bound model underneath, which
is the only combination that can reproduce it. The deterministic stage fakes call no model
at all, so they have no budget to exhaust — which is precisely why every earlier
end-to-end test was green while this was broken.

Against a real Postgres, like the rest of ``workers/tests``.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from motet_db import EpisodeState, repo
from motet_inference.adapters import ClaudeGroundingValidator, ClaudeScriptGenerator
from motet_inference.interfaces import Stages
from motet_inference.llm import LlmBudgetExhaustedError, LlmRequest, LlmResponse, Usage
from motet_inference.registry import fake_stages
from motet_storage import LocalObjectStore
from motet_workers import Queue, drain, enqueue_episode, enqueue_paste

USER = repo.OWNER_USER_ID

#: The episode that died on staging. Sized to the failure rather than to the test.
BACKLOG = 19

#: The ceiling the grounding stage used to carry, whatever the size of the episode.
OLD_FIXED_CEILING = 8_000


def newsletter(index: int) -> tuple[str, str]:
    """One pasted item: a headline and four sentences, three of them quotable."""
    return (
        f"Chipmaker {index} posts quarterly results",
        f"Chipmaker {index} posts quarterly results. "
        f"The company reported revenue of ${index + 10} million for the quarter. "
        f"It said it would hire {index * 3} engineers over the next year. "
        f"The filing was published on Tuesday, according to the regulator.",
    )


class BudgetBoundModel:
    """A model that thinks per claim, and cannot answer a call it outspends.

    motet#42 reduced to arithmetic. Staging came back with
    ``output_tokens == reasoning_tokens == 8000`` and not one verdict for a request
    carrying twelve claims, so reasoning cost at least ~660 tokens a claim and the answer
    never started. Here it costs ``reasoning_per_claim``, and a call whose ceiling runs
    out first raises exactly what the OpenRouter adapter raises.

    It answers the script call unconditionally: scripting is one call per episode with a
    32k ceiling and was never the stage in trouble.
    """

    reasoning_per_claim = 700
    answer_per_claim = 40

    def __init__(self, script_payload: object) -> None:
        self._script = json.dumps(script_payload)
        self.grounding_calls: list[int] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        name = request.response_format.name if request.response_format else ""
        if name != "grounding_verdicts":
            return self._respond(self._script, request)

        prompt = request.messages[-1].text
        claims = sum(1 for line in prompt.splitlines() if line.startswith("CLAIM "))
        self.grounding_calls.append(claims)
        spent = (self.reasoning_per_claim + self.answer_per_claim) * claims
        if spent > request.max_output_tokens:
            raise LlmBudgetExhaustedError(
                f"{claims} claims would spend {spent} of {request.max_output_tokens}"
            )
        return self._respond(
            json.dumps(
                {
                    "verdicts": [
                        {"index": index, "supported": True, "reason": ""} for index in range(claims)
                    ]
                }
            ),
            request,
        )

    @staticmethod
    def _respond(text: str, request: LlmRequest) -> LlmResponse:
        return LlmResponse(
            text=text,
            model=request.model,
            usage=Usage(output_tokens=len(text.split())),
            reasoning_applied=request.reasoning is not None,
            finish_reason="stop",
        )


def script_payload(db: psycopg.Connection[Any]) -> tuple[object, int]:
    """A script covering every news item, quoting its source verbatim.

    Written from what is actually in the database rather than from constants: the point of
    this test is the *number* of claims that reach the gate, and a quote the script stage
    could not locate would silently reduce it.
    """
    news_items = repo.list_news_items(db, USER)
    sources = repo.load_source_items(
        db, [sid for item in news_items for sid in item.source_item_ids]
    )
    segments = []
    claims_written = 0
    for item in news_items:
        source_id = item.source_item_ids[0]
        source = sources[source_id]
        quotes = [sentence.strip() for sentence in source.text.split(". ")][1:4]
        claims_written += len(quotes)
        segments.append(
            {
                "news_item_id": item.id,
                "claims": [
                    {
                        "text": f"Here is the news: {quote}",
                        "quote": quote,
                        "source_item_id": source_id,
                    }
                    for quote in quotes
                ],
            }
        )
    return {"segments": segments}, claims_written


def install(monkeypatch: pytest.MonkeyPatch, model: BudgetBoundModel) -> None:
    """Real script and grounding adapters over ``model``; fakes for everything else."""
    import motet_workers.loop as loop

    base = fake_stages()
    monkeypatch.setattr(
        loop,
        "get_stages",
        lambda: Stages(
            integrator=base.integrator,
            script_generator=ClaudeScriptGenerator(model),
            grounding_validator=ClaudeGroundingValidator(model),
            speech_synthesizer=base.speech_synthesizer,
        ),
    )


def test_a_nineteen_item_backlog_scripts_grounds_and_renders(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: LocalObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The journey that produced no audio: paste a morning's reading, get an episode.

    Every assertion below held before the fix except the ones about grounding: the script
    stage wrote its script, and then the gate could not return a single verdict.
    """
    for index in range(BACKLOG):
        title, text = newsletter(index)
        enqueue_paste(db, user_id=USER, title=title, text=text)
    db.commit()
    drain(Queue.INTEGRATE, _migrated)
    assert len(repo.list_news_items(db, USER)) == BACKLOG

    episode_id = enqueue_episode(
        db, user_id=USER, title="Morning briefing", max_duration_ms=90 * 60_000
    )
    db.commit()
    assert drain(Queue.ASSEMBLE, _migrated) == 1

    payload, claims_written = script_payload(db)
    # The failure this test exists for, stated as the arithmetic that produces it: one
    # call carrying every claim needs more thinking than the ceiling the stage used to
    # carry, and that ceiling did not move when the backlog grew.
    assert BudgetBoundModel.reasoning_per_claim * claims_written > OLD_FIXED_CEILING

    model = BudgetBoundModel(payload)
    install(monkeypatch, model)

    assert drain(Queue.SCRIPT, _migrated) == 1
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.state is EpisodeState.RENDERING
    # Nothing was dropped: every claim was judged, in a call small enough to be answered.
    assert sum(len(segment.claims) for segment in episode.segments) == claims_written
    assert len(model.grounding_calls) > 1
    assert sum(model.grounding_calls) == claims_written

    assert drain(Queue.TTS, _migrated) == 1
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.state is EpisodeState.READY
    assert episode.audio_bytes and episode.audio_bytes > 0
    assert episode.audio_key is not None
    assert object_store.exists(episode.audio_key)


def test_a_claim_the_gate_cannot_afford_costs_that_claim_and_not_the_episode(
    db: psycopg.Connection[Any],
    _migrated: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor of the halving, at the level where it matters: the episode still ships.

    A budget so small that no call can ever be answered is the worst case the stage has.
    Before the fix it lost the episode. Now every claim fails closed — none of them is
    spoken, which is invariant 3 — and ``handle_script`` says so loudly, which is the
    existing rule for an episode where nothing survived the gate.
    """
    for index in range(3):
        title, text = newsletter(index)
        enqueue_paste(db, user_id=USER, title=title, text=text)
    db.commit()
    drain(Queue.INTEGRATE, _migrated)
    episode_id = enqueue_episode(
        db, user_id=USER, title="Morning briefing", max_duration_ms=90 * 60_000
    )
    db.commit()
    drain(Queue.ASSEMBLE, _migrated)

    payload, _ = script_payload(db)
    model = BudgetBoundModel(payload)
    # Ruinous: one claim alone would outspend any ceiling the validator can ask for.
    model.reasoning_per_claim = 10_000_000
    install(monkeypatch, model)

    drain(Queue.SCRIPT, _migrated)

    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    # Not rendering — nothing was grounded, so nothing may be spoken — but the failure is
    # a reported one rather than a retry loop, and it named every claim it could not
    # judge rather than dying inside the model call.
    assert episode.state is not EpisodeState.RENDERING
    assert model.grounding_calls  # it kept halving instead of giving up on the first call
    assert min(model.grounding_calls) == 1
