"""``spoken_through_ms`` — ours, not the provider's. Invariant 4, made a type.

This is the smallest module in the service and the one with the most rules attached, so
they are written here rather than in a review comment:

**We own playback position.** :class:`PlaybackClock` advances from a monotonic time source
this process controls, gated on whether narration is actually playing, and clamped to how
much audio we have handed to the client. Nothing a provider says moves it.

**A provider's position is evidence, never state.** :meth:`PlaybackClock.note_provider_position`
exists so that drift can be *measured* — and the reason to measure it is exactly the reason
not to trust it. A realtime provider's notion of position is its own generation cursor. It
diverges from what the listener heard the instant an interruption happens, because the
provider stops generating at one offset while the client is still playing out a buffer that
ends at another. That divergence is not a bug in the provider; it is two different clocks,
and only one of them is the one a highlight anchors to.

**Interruption freezes, it does not reset.** ``interrupted_at`` is the position at the
moment of the barge-in, which is what the client is told, what a highlight anchors to, and
where narration resumes. Recomputing it later from a buffer length gets a different number.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic

logger = logging.getLogger("motet.voice.clock")

#: Report drift above this when a provider tells us where it thinks we are. Chosen to be
#: bigger than jitter and smaller than a sentence: below it, two clocks disagree about
#: buffering; above it, they disagree about what the listener heard.
DRIFT_WARN_MS = 400


@dataclass
class PlaybackClock:
    """How far into the narration the listener has actually heard.

    ``now`` is injectable so tests and offline replay are deterministic — a replay of a
    captured walk must produce the same offsets on every run, and it cannot do that off a
    wall clock.
    """

    now: Callable[[], float] = monotonic
    _spoken_through_ms: int = 0
    _delivered_ms: int = 0
    _playing_since: float | None = None
    _interrupted_at_ms: int | None = None
    _provider_drifts_ms: list[int] = field(default_factory=list)

    # -- what we know -----------------------------------------------------------------

    @property
    def spoken_through_ms(self) -> int:
        """The invariant's namesake: how much narration the listener has heard."""
        return self._settle()

    @property
    def delivered_ms(self) -> int:
        """How much narration audio has been handed to the transport."""
        return self._delivered_ms

    @property
    def playing(self) -> bool:
        return self._playing_since is not None

    @property
    def interrupted_at_ms(self) -> int | None:
        """Where the last barge-in landed, or ``None`` if none has happened."""
        return self._interrupted_at_ms

    @property
    def max_provider_drift_ms(self) -> int:
        """The worst disagreement seen between our clock and a provider's.

        Reported on the session summary. A large number is not an error — it is the
        evidence for invariant 4, and it is the kind of thing that is only ever noticed
        if something writes it down.
        """
        return max((abs(drift) for drift in self._provider_drifts_ms), default=0)

    # -- what moves it ----------------------------------------------------------------

    def deliver(self, chunk_ms: int) -> None:
        """Record narration audio handed to the client.

        This raises the ceiling; it does not advance the position. Delivering ten seconds
        of audio in one burst does not mean ten seconds have been heard, and treating it
        as if it did is how a resume offset ends up past the end of what was played.
        """
        if chunk_ms < 0:
            raise ValueError("chunk_ms cannot be negative")
        self._delivered_ms += chunk_ms

    def start(self) -> None:
        """Narration began, or resumed after an interruption."""
        if self._playing_since is None:
            self._playing_since = self.now()

    def pause(self) -> None:
        """Narration stopped for a reason that is not a barge-in."""
        self._settle()
        self._playing_since = None

    def interrupt(self) -> int:
        """Freeze at the barge-in and return the offset the client is told.

        The returned value is ``interrupted_at(offset)`` on the wire. It is captured here,
        once, at the moment of the decision — every later consumer reads this number rather
        than recomputing its own.
        """
        offset = self._settle()
        self._playing_since = None
        self._interrupted_at_ms = offset
        return offset

    def seek(self, position_ms: int) -> None:
        """Move to an explicit position — a client resuming an episode, for instance.

        The one legitimate way an outside party sets the clock, and it is deliberately not
        the provider: it is the *client* reporting where its own player is, which is the
        only external actor that knows what came out of the speaker.

        Seeking also raises the delivered ceiling, because a client that played to ``X``
        is telling us ``X`` was delivered — by an earlier session, or straight out of the
        batch-narration file it downloaded. Without that, resuming an episode this process
        has not itself streamed would be clamped straight back to zero.
        """
        if position_ms < 0:
            raise ValueError("position_ms cannot be negative")
        self._settle()
        self._spoken_through_ms = position_ms
        self._delivered_ms = max(self._delivered_ms, position_ms)
        if self._playing_since is not None:
            self._playing_since = self.now()

    def note_provider_position(self, provider_position_ms: int) -> int:
        """Record what a provider thinks the position is. Returns the drift.

        **This never mutates the clock.** If it ever does, invariant 4 is gone and nothing
        in the test suite would notice, which is why the assertion lives in
        ``tests/test_clock.py`` rather than in a comment here.
        """
        drift = provider_position_ms - self._settle()
        self._provider_drifts_ms.append(drift)
        if abs(drift) >= DRIFT_WARN_MS:
            logger.warning(
                "provider playback position differs from ours by %dms "
                "(provider=%dms, ours=%dms) — ours is authoritative (invariant 4)",
                drift,
                provider_position_ms,
                self._spoken_through_ms,
            )
        return drift

    # -- internals --------------------------------------------------------------------

    def _settle(self) -> int:
        """Fold elapsed playing time into the position, clamped to what was delivered."""
        if self._playing_since is not None:
            # One reading, used for both the elapsed span and the new mark. Two calls would
            # silently discard whatever passed between them, and the loss compounds because
            # this runs on every frame.
            now = self.now()
            elapsed_ms = int((now - self._playing_since) * 1000)
            if elapsed_ms > 0:
                self._spoken_through_ms += elapsed_ms
                self._playing_since = now
        self._spoken_through_ms = min(self._spoken_through_ms, self._delivered_ms)
        return self._spoken_through_ms
