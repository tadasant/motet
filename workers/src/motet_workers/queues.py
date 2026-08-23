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
