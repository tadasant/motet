"""``MOTET_GMAIL_FIRST_SYNC_DAYS`` — how far back a mailbox's first sync reaches.

An optional knob (motet#91, #94): unset, the default holds, and every value that is not a
positive integer falls back to it rather than to a Gmail search nobody meant.
"""

from __future__ import annotations

import pytest
from motet_sources.gmail import DEFAULT_FIRST_SYNC_DAYS, FIRST_SYNC_DAYS_ENV, first_sync_days


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_FIRST_SYNC_DAYS),
        ("", DEFAULT_FIRST_SYNC_DAYS),
        ("60", 60),
        (" 30 ", 30),
        ("0", DEFAULT_FIRST_SYNC_DAYS),
        ("-5", DEFAULT_FIRST_SYNC_DAYS),
        ("a week", DEFAULT_FIRST_SYNC_DAYS),
    ],
)
def test_first_sync_days(raw: str | None, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    if raw is None:
        monkeypatch.delenv(FIRST_SYNC_DAYS_ENV, raising=False)
    else:
        monkeypatch.setenv(FIRST_SYNC_DAYS_ENV, raw)
    assert first_sync_days() == expected
