"""Tests for the worker scaffold.

The queue names are a contract between whatever enqueues and whatever drains, so they get
pinned even though no worker runs yet.
"""

from __future__ import annotations

import pytest
from motet_workers import Queue, drain


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
