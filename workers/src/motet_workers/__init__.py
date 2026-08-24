"""Ingestion and pipeline workers (Cloud Run jobs).

`Integrate → Assemble → Script + grounding → TTS → object storage`. Each stage is its own
queue on the one Postgres ``jobs`` table, drained by its own Cloud Run job, because the
stages have different rate limits and failure modes.
"""

from .handlers import (
    Context,
    PermanentFailure,
    apportion_claim_timings,
    enqueue_episode,
    enqueue_paste,
    enqueue_smart_episode,
)
from .ingest import enqueue_source_poll, poll_key
from .jobs import Job, enqueue, queue_depths
from .queues import Queue
from .runner import drain

__all__ = [
    "Context",
    "Job",
    "PermanentFailure",
    "Queue",
    "apportion_claim_timings",
    "drain",
    "enqueue",
    "enqueue_episode",
    "enqueue_paste",
    "enqueue_smart_episode",
    "enqueue_source_poll",
    "poll_key",
    "queue_depths",
]
