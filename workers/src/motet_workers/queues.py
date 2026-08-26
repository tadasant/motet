"""The pipeline's queue names.

`Poll → Extract → Dedup/Integrate → Assemble → Script + grounding → TTS → GCS`.

Each stage is a separate queue on the one ``jobs`` table because the stages have different
rate limits and failure modes — a Cartesia 429 must not stall dedup. They are named here,
in one place, so a worker and whatever enqueues to it cannot disagree about the string.
"""

from __future__ import annotations

from enum import StrEnum


class Queue(StrEnum):
    POLL = "poll"
    EXTRACT = "extract"
    INTEGRATE = "integrate"
    ASSEMBLE = "assemble"
    SCRIPT = "script"
    TTS = "tts"


#: Every queue, in the order work travels through them.
#:
#: A single always-on worker drains this list top to bottom on each pass, so one pass
#: carries a pasted item as far as it can go: integrate writes the news item, and the
#: assemble/script/tts jobs an episode enqueues are picked up on the same sweep rather
#: than one pass later. Ordering it backwards would still work and would add a full poll
#: interval per stage, which on a five-stage pipeline is the difference between "a few
#: seconds" and "why is nothing happening".
#:
#: Declared here rather than in the runner because a queue added to the enum and forgotten
#: here is a queue nothing drains — the exact failure motet#38 is about, one stage down.
PIPELINE: tuple[Queue, ...] = (
    Queue.POLL,
    Queue.EXTRACT,
    Queue.INTEGRATE,
    Queue.ASSEMBLE,
    Queue.SCRIPT,
    Queue.TTS,
)
