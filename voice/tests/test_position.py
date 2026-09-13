"""Where the listener interrupted, from our clock and the caller's timed transcript."""

from __future__ import annotations

from motet_voice.contract import TimedClaim, TimedSegment
from motet_voice.position import RECENT_WINDOW_MS, locate, position_notes


def _transcript() -> list[TimedSegment]:
    """Three stories; the second has three claims, the third has never been reached."""
    return [
        TimedSegment(
            title="Acme raises a Series B",
            start_ms=0,
            end_ms=20_000,
            claims=[
                TimedClaim(start_ms=0, end_ms=10_000, spoken_text="Acme raised forty million."),
                TimedClaim(start_ms=10_000, end_ms=20_000, spoken_text="Example Ventures led."),
            ],
        ),
        TimedSegment(
            title="Helion's reactor timeline",
            start_ms=20_000,
            end_ms=50_000,
            claims=[
                TimedClaim(start_ms=20_000, end_ms=30_000, spoken_text="Helion targets 2028."),
                TimedClaim(start_ms=30_000, end_ms=40_000, spoken_text="It raised 425 million."),
                TimedClaim(
                    start_ms=40_000, end_ms=50_000, spoken_text="Microsoft is the first customer."
                ),
            ],
        ),
        TimedSegment(
            title="A court ruling on voter rolls",
            start_ms=50_000,
            end_ms=70_000,
            claims=[
                TimedClaim(start_ms=50_000, end_ms=70_000, spoken_text="185 IDs were flagged.")
            ],
        ),
    ]


def test_the_block_names_the_segment_and_claim_under_the_clock() -> None:
    """Inside claim 3 of segment 2: that claim, segment 1 heard, segment 3 not reached."""
    notes = position_notes(_transcript(), 47_000)

    assert "at 0:47" in notes
    assert "during the story 'Helion's reactor timeline'" in notes
    assert "while this was being said: 'Microsoft is the first customer.'" in notes
    assert "Earlier stories already heard: Acme raises a Series B." in notes
    assert "Stories not yet reached: A court ruling on voter rolls." in notes
    assert "assume they mean what was just said" in notes


def test_the_recent_window_quotes_what_was_just_said_and_not_the_whole_episode() -> None:
    position = locate(_transcript(), 47_000)

    assert position.claim is not None
    assert position.claim.spoken_text == "Microsoft is the first customer."
    # 47 s back to 22 s: the three Helion claims overlap the window; Acme's do not.
    assert position.recent == (
        "Helion targets 2028.",
        "It raised 425 million.",
        "Microsoft is the first customer.",
    )
    assert [claim.spoken_text for claim in position.heard] == [
        "Acme raised forty million.",
        "Example Ventures led.",
        "Helion targets 2028.",
        "It raised 425 million.",
    ], "a claim is heard when its end has passed, and the current one has not"
    assert RECENT_WINDOW_MS == 25_000


def test_no_transcript_means_no_block() -> None:
    """The harness and every existing test send none, and must be prompted as before."""
    assert position_notes([], 12_345) == ""
    assert locate([], 12_345).to_json() == {}


def test_a_boundary_offset_belongs_to_the_segment_that_is_starting() -> None:
    position = locate(_transcript(), 20_000)
    assert position.segment is not None
    assert position.segment.title == "Helion's reactor timeline"
    assert position.claim is not None
    assert position.claim.spoken_text == "Helion targets 2028."
    assert position.earlier_titles == ("Acme raises a Series B",)


def test_past_the_end_still_points_at_the_last_story() -> None:
    """Interrupting in the closing silence is still about the story that just ended."""
    position = locate(_transcript(), 75_000)
    assert position.segment is not None
    assert position.segment.title == "A court ruling on voter rolls"
    assert position.claim is not None
    assert position.claim.spoken_text == "185 IDs were flagged."
    assert position.later_titles == ()
    assert "Stories not yet reached: none — this is the last." in position_notes(
        _transcript(), 75_000
    )


def test_the_client_facing_context_is_the_same_facts_the_model_gets() -> None:
    assert locate(_transcript(), 33_000).to_json() == {
        "clock": "0:33",
        "segment_title": "Helion's reactor timeline",
        "claim_text": "It raised 425 million.",
    }
