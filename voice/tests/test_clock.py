"""``spoken_through_ms`` is ours — invariant 4, asserted rather than asserted about."""

from __future__ import annotations

import pytest
from motet_voice.clock import DRIFT_WARN_MS, PlaybackClock


class FakeTime:
    """A monotonic source a test can drive."""

    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_position_advances_only_while_playing() -> None:
    now = FakeTime()
    clock = PlaybackClock(now=now)
    clock.deliver(60_000)

    now.advance(2.0)
    assert clock.spoken_through_ms == 0, "time passing before playback started must not count"

    clock.start()
    now.advance(3.0)
    assert clock.spoken_through_ms == pytest.approx(3_000, abs=5)

    clock.pause()
    now.advance(10.0)
    assert clock.spoken_through_ms == pytest.approx(3_000, abs=5)


def test_position_is_clamped_to_delivered_audio() -> None:
    now = FakeTime()
    clock = PlaybackClock(now=now)
    clock.deliver(1_500)
    clock.start()
    now.advance(30.0)
    assert clock.spoken_through_ms == 1_500, "cannot have heard more than was ever sent"


def test_interrupt_freezes_and_reports_the_offset() -> None:
    now = FakeTime()
    clock = PlaybackClock(now=now)
    clock.deliver(120_000)
    clock.start()
    now.advance(7.5)

    offset = clock.interrupt()
    assert offset == pytest.approx(7_500, abs=5)
    assert clock.interrupted_at_ms == offset

    now.advance(60.0)
    assert clock.spoken_through_ms == offset, "an interrupted clock does not keep running"

    clock.start()
    now.advance(1.0)
    assert clock.spoken_through_ms == pytest.approx(offset + 1_000, abs=5), "resume continues"


def test_a_provider_position_never_moves_our_clock() -> None:
    """The invariant, stated as the single most important assertion in this file."""
    now = FakeTime()
    clock = PlaybackClock(now=now)
    clock.deliver(120_000)
    clock.start()
    now.advance(5.0)
    ours = clock.spoken_through_ms

    drift = clock.note_provider_position(99_000)

    assert clock.spoken_through_ms == pytest.approx(ours, abs=5)
    assert drift == pytest.approx(99_000 - ours, abs=5)
    assert clock.max_provider_drift_ms >= DRIFT_WARN_MS


def test_client_reported_position_may_seek_past_what_this_process_delivered() -> None:
    """Resuming an episode this instance never streamed is the normal Cloud Run case."""
    clock = PlaybackClock(now=FakeTime())
    clock.seek(600_000)
    assert clock.spoken_through_ms == 600_000


def test_negative_inputs_are_rejected() -> None:
    clock = PlaybackClock(now=FakeTime())
    with pytest.raises(ValueError):
        clock.deliver(-1)
    with pytest.raises(ValueError):
        clock.seek(-1)
