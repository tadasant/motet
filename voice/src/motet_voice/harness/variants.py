"""The config sweep: which points in policy space one walk gets to test.

**This is what makes a single walk worth more than a single answer.** A live A/B outdoors
tests one configuration per walk and takes the weather with it; a recording tests as many
as there are entries below, against byte-identical audio, in seconds.

The grid is small on purpose. Every extra variant is another column in a report someone
reads on a phone, and the dial that matters most — how many consecutive frames of apparent
speech it takes to commit — is the one varied most finely.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ..bargein import BargeInPolicy

#: What the service ships with unless configured otherwise. Present in the sweep so the
#: report always has the deployed setting as its baseline column.
DEFAULT_VARIANT: Final = BargeInPolicy(name="default")

#: Ordered from twitchiest to most deliberate. The names are the units a human thinks in —
#: "how long do I have to be talking before it stops" — rather than frame counts.
SWEEP: Final[tuple[BargeInPolicy, ...]] = (
    BargeInPolicy(
        name="hair-trigger-60ms",
        consecutive_speech_frames=3,
        speech_probability_threshold=0.5,
        min_snr_db=8.0,
    ),
    DEFAULT_VARIANT,
    BargeInPolicy(
        name="steady-240ms",
        consecutive_speech_frames=12,
        speech_probability_threshold=0.65,
        min_snr_db=14.0,
    ),
    BargeInPolicy(
        name="patient-400ms",
        consecutive_speech_frames=20,
        speech_probability_threshold=0.7,
        min_snr_db=16.0,
    ),
    # The same patience, but insisting on a much louder signal relative to the noise floor.
    # This is the variant that should win outdoors if wind is the dominant adversary, and
    # the one that should lose indoors — which is exactly the sort of thing a walk settles.
    BargeInPolicy(
        name="loud-and-patient",
        consecutive_speech_frames=12,
        speech_probability_threshold=0.75,
        min_snr_db=22.0,
    ),
)


def sweep(only: Sequence[str] = ()) -> tuple[BargeInPolicy, ...]:
    """The variants to replay, optionally filtered by name."""
    if not only:
        return SWEEP
    wanted = {name.strip() for name in only if name.strip()}
    chosen = tuple(policy for policy in SWEEP if policy.name in wanted)
    if not chosen:
        known = ", ".join(policy.name for policy in SWEEP)
        raise ValueError(f"no variant matched {sorted(wanted)}; known variants: {known}")
    return chosen
