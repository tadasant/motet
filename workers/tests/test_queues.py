"""Tests for the worker scaffold.

The queue names are a contract between whatever enqueues and whatever drains, so they get
pinned even though no worker runs yet.
"""

from __future__ import annotations

import pytest
from motet_inference.llm import LlmConfigError
from motet_workers import Queue, drain, runner


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


def test_drain_is_not_built_yet() -> None:
    with pytest.raises(NotImplementedError):
        drain(Queue.POLL, "postgresql://unused")


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
    """Config validation must not need a key in CI or on a laptop."""
    monkeypatch.delenv("MOTET_INFERENCE_MODE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(NotImplementedError, match="factory scaffold"):
        runner.main(["integrate"])
