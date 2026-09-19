"""Ingestion and pipeline workers (Cloud Run jobs).

`Poll → Extract → Enrich → Integrate → Assemble → Script → TTS → object storage`. Each
stage is its own queue on the one Postgres ``jobs`` table, drained by its own Cloud Run
job, because the stages have different rate limits and failure modes.

**Nothing here may import** :mod:`motet_workers.runner`. That module is the image's
``ENTRYPOINT``, so ``python -m`` executes it — and a module that this file has already
imported gets executed a *second* time, under a second name, with a second copy of every
module-level object. :func:`drain` therefore lives in :mod:`motet_workers.loop`, which is
importable, and ``runner`` holds only the CLI. See motet#21.
"""

from .enrich import (
    EnrichConfig,
    EnrichTarget,
    enrichment_sites,
    plan_enrichment,
    source_item_links,
)
from .handlers import (
    Context,
    PermanentFailure,
    apportion_claim_timings,
    enqueue_episode,
    enqueue_integrate_job,
    enqueue_integration,
    enqueue_paste,
    enqueue_smart_episode,
)
from .ingest import enqueue_source_poll, poll_key, source_query
from .jobs import DEFAULT_MAX_ATTEMPTS, Job, QueueReadiness, enqueue, queue_depths, queue_readiness
from .loop import drain
from .queues import Queue

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Context",
    "EnrichConfig",
    "EnrichTarget",
    "Job",
    "PermanentFailure",
    "Queue",
    "QueueReadiness",
    "apportion_claim_timings",
    "drain",
    "enqueue",
    "enqueue_episode",
    "enqueue_integrate_job",
    "enqueue_integration",
    "enqueue_paste",
    "enqueue_smart_episode",
    "enqueue_source_poll",
    "enrichment_sites",
    "plan_enrichment",
    "poll_key",
    "queue_depths",
    "queue_readiness",
    "source_item_links",
    "source_query",
]
