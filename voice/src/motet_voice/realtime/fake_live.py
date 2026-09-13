"""A live conversation with no vendor behind it — the fake-mode realtime arm (invariant 7).

The realtime arm's live channel is the one part of the voice service a person has to *feel*
to judge: the first word arriving early, talking over a reply, narration picking up where it
stopped. None of that is visible in a unit test, and the real channel is billed per audio
token. This fake makes the whole loop runnable in a browser for free, on
``MOTET_VOICE_ARM=openai_realtime`` with ``MOTET_INFERENCE_MODE=fake``.

It imitates the *shape* of the vendor's side, not its intelligence:

* **End of utterance** is a fixed-threshold energy gate with the vendor's documented
  ``silence_duration_ms`` — speech, then half a second of quiet, and the turn is over. It is
  not the vendor's model and nothing measured through it is a measurement of the vendor.
* **The reply** is a short tone streamed in 100 ms chunks, and a sentence that repeats the
  position block back — so a tester can see that the interruption point reached the model,
  which is the property the real channel is trusted for.
* **Talking over the reply** cancels it, the way the vendor's ``interrupt_response`` does.

Deterministic apart from wall-clock pacing, and it reaches nothing outside the process.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

from ..audio import DEFAULT_FRAME_MS, TARGET_SAMPLE_RATE, dbfs, samples_from_pcm
from .interfaces import (
    AssistantAudio,
    AssistantTranscript,
    LiveEvent,
    SpeechStarted,
    SpeechStopped,
    TurnDone,
    TurnRequest,
    UserTranscript,
)

#: The fake's reply is spoken at the vendor's rate, so the client's pcm16 path is the one
#: exercised.
REPLY_SAMPLE_RATE: Final = 24_000
#: A frame at or above this level is "speech" to the fake. Fixed rather than adaptive: the
#: session's own detector is the adaptive one, and this only has to find the end of an
#: utterance that detector already decided had started.
SPEECH_DBFS: Final = -45.0
#: The vendor's documented default for how much quiet ends a turn.
SILENCE_TO_END_MS: Final = 500
#: An utterance that never goes quiet is answered anyway, so a stuck mic cannot hold the
#: floor forever.
MAX_UTTERANCE_MS: Final = 10_000
REPLY_CHUNK_MS: Final = 100
REPLY_MS: Final = 1_500
#: Pause between chunks. Shorter than a chunk, so the client's queue runs ahead the way it
#: does against the vendor.
CHUNK_PACING_SECONDS: Final = 0.04

_POSITION = re.compile(
    r"interrupted the briefing at (?P<clock>\d+:\d\d), during the story '(?P<title>[^']*)'"
)

_FRAME_BYTES: Final = TARGET_SAMPLE_RATE * DEFAULT_FRAME_MS // 1000 * 2


class FakeLiveConversation:
    """One session's fake live channel. See the module docstring."""

    def __init__(self, *, history: Sequence[Mapping[str, str]] = ()) -> None:
        self._queue: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self._position = ""
        self._residue = b""
        self._heard_ms = 0
        self._speech_ms = 0
        self._quiet_ms = 0
        self._in_speech = False
        self._reply: asyncio.Task[None] | None = None
        self._replies = 0
        self.history = list(history)
        self.truncated: list[tuple[str, int]] = []
        self.cancelled = 0
        self.closed = False

    @classmethod
    def for_request(cls, request: TurnRequest) -> FakeLiveConversation:
        return cls(history=request.history)

    async def start(self) -> None:
        return None

    async def append_audio(self, pcm: bytes) -> None:
        buffer = self._residue + pcm
        usable = len(buffer) - (len(buffer) % _FRAME_BYTES)
        self._residue = buffer[usable:]
        for offset in range(0, usable, _FRAME_BYTES):
            self._observe_frame(buffer[offset : offset + _FRAME_BYTES])

    def _observe_frame(self, frame: bytes) -> None:
        level = dbfs(samples_from_pcm(frame))
        self._heard_ms += DEFAULT_FRAME_MS
        speech = level >= SPEECH_DBFS
        if not self._in_speech:
            if not speech:
                return
            self._in_speech = True
            self._speech_ms = 0
            self._quiet_ms = 0
            if self._reply is not None and not self._reply.done():
                # Talked over: the vendor cuts its own reply off.
                self._reply.cancel()
                self._reply = None
                self._queue.put_nowait(SpeechStarted(audio_start_ms=self._heard_ms))
                self._queue.put_nowait(TurnDone(cancelled=True))
                return
            self._queue.put_nowait(SpeechStarted(audio_start_ms=self._heard_ms))
            return
        self._speech_ms += DEFAULT_FRAME_MS
        self._quiet_ms = 0 if speech else self._quiet_ms + DEFAULT_FRAME_MS
        if self._quiet_ms >= SILENCE_TO_END_MS or self._speech_ms >= MAX_UTTERANCE_MS:
            self._in_speech = False
            spoken_s = max(0, self._speech_ms - self._quiet_ms) / 1000
            self._queue.put_nowait(SpeechStopped(audio_end_ms=self._heard_ms))
            self._queue.put_nowait(
                UserTranscript(text=f"(fake transcript: {spoken_s:.1f} s of speech)")
            )
            self._start_reply(question=None)

    async def add_context(self, text: str) -> None:
        if text.strip():
            self._position = text

    async def add_user_text(self, text: str) -> None:
        self._start_reply(question=text)

    async def tool_output(self, call_id: str, output: Mapping[str, Any]) -> None:
        self._queue.put_nowait(TurnDone())

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.truncated.append((item_id, audio_end_ms))

    async def cancel_response(self) -> None:
        self.cancelled += 1
        self._in_speech = False
        if self._reply is not None and not self._reply.done():
            self._reply.cancel()
            self._reply = None
            self._queue.put_nowait(TurnDone(cancelled=True))

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def aclose(self) -> None:
        self.closed = True
        if self._reply is not None:
            self._reply.cancel()
        self._queue.put_nowait(None)

    # -- the reply ----------------------------------------------------------------------

    def _start_reply(self, *, question: str | None) -> None:
        self._replies += 1
        item_id = f"fake_item_{self._replies}"
        self._reply = asyncio.create_task(self._speak(item_id, self._reply_text(question)))

    def _reply_text(self, question: str | None) -> str:
        match = _POSITION.search(self._position)
        where = (
            f"You stopped me at {match['clock']}, in '{match['title']}'."
            if match
            else "I have no position for this turn."
        )
        asked = f" You asked: {question.strip()}" if question and question.strip() else ""
        return f"{where}{asked} This is the fake live arm, so there is no real answer."

    async def _speak(self, item_id: str, text: str) -> None:
        for chunk in _tone_chunks():
            self._queue.put_nowait(
                AssistantAudio(pcm=chunk, sample_rate=REPLY_SAMPLE_RATE, item_id=item_id)
            )
            await asyncio.sleep(CHUNK_PACING_SECONDS)
        self._queue.put_nowait(AssistantTranscript(text=text, item_id=item_id))
        self._queue.put_nowait(TurnDone(usage={}))


def _tone_chunks() -> list[bytes]:
    """A quiet 440 Hz tone with a fade at each end, in ``REPLY_CHUNK_MS`` chunks."""
    total = REPLY_SAMPLE_RATE * REPLY_MS // 1000
    per_chunk = REPLY_SAMPLE_RATE * REPLY_CHUNK_MS // 1000
    fade = REPLY_SAMPLE_RATE // 50
    samples = bytearray()
    for index in range(total):
        envelope = min(1.0, index / fade, (total - index) / fade)
        value = int(4_000 * envelope * math.sin(2 * math.pi * 440 * index / REPLY_SAMPLE_RATE))
        samples += int.to_bytes(value & 0xFFFF, 2, "little")
    step = per_chunk * 2
    return [bytes(samples[offset : offset + step]) for offset in range(0, len(samples), step)]
