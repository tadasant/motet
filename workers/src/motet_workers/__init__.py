"""Ingestion and pipeline workers (Cloud Run jobs).

`Integrate → Assemble → Script + grounding → TTS → object storage`. Each stage is its own
queue on the one Postgres ``jobs`` table, drained by its own Cloud Run job, because the
stages have different rate limits and failure modes.

**Nothing here may import** :mod:`motet_workers.runner`. That module is the image's
``ENTRYPOINT``, so ``python -m`` executes it — and a module that this file has already
imported gets executed a *second* time, under a second name, with a second copy of every
module-level object. :func:`drain` therefore lives in :mod:`motet_workers.drain`, which is
importable, and ``runner`` holds only the CLI. See motet#21.
"""

from .drain import drain
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
