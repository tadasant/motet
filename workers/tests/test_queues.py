"""The worker's queue names and entry-point guards.

The queue names are a contract between whatever enqueues and whatever drains, so they get
pinned. The pipeline behaviour itself lives in ``test_pipeline.py``, which needs a database.
"""

from __future__ import annotations

import pytest
from motet_inference.llm import LlmConfigError
from motet_workers import Queue, runner


def test_queue_names_cover_every_pipeline_stage() -> None:
    assert [q.value for q in Queue] == [
        "poll",
        "extract",
        "integrate",
        "assemble",
        "script",
        "tts",
    ]


def test_queues_are_plain_strings() -> None:
    """They go into a `text` column and into SQL literals — they must compare as str."""
    assert Queue.TTS == "tts"


def test_every_queue_has_a_handler() -> None:
    """`poll` and `extract` were named in the enum from the start and now have handlers.

    Asserted as a set rather than per queue, so adding a queue without a handler fails here
    instead of reporting "drained 0 jobs" forever — which is what a misconfigured worker
    pointed at an unimplemented stage would otherwise look like.
    """
    from motet_workers.handlers import HANDLERS

    assert set(HANDLERS) == set(Queue), (
        "a queue with no handler is a worker that silently does nothing"
    )


def test_the_worker_refuses_to_start_without_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Cloud Run job that cannot reach a model fails before it claims any work.

    This is where the credential check lives: workers are what actually call a model, so
    they are what must hold the key. Discovering it missing after claiming a job means a
    job left half-done and a failure far from its cause.
    """
    monkeypatch.setenv("MOTET_INFERENCE_MODE", "real")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LlmConfigError, match="OPENROUTER_API_KEY"):
        runner.main(["integrate"])


def test_the_worker_starts_with_no_credential_when_inference_is_faked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config validation must not need a key in CI or on a laptop.

    It still needs somewhere to drain *from*, so with no database configured the entry
    point stops on that rather than on a missing key — which is the point: the credential
    was never the obstacle.
    """
    monkeypatch.delenv("MOTET_INFERENCE_MODE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        runner.main(["integrate"])
