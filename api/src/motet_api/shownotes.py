"""Show notes, chapters, and subtitles — all three derived from the transcript we keep.

Nothing here is new information. An episode already stores its segments in spoken order,
each with a story and a set of claims, and each claim already carries the source span that
evidences it (invariant 3). That structure *is* a transcript with citations; this module
renders it in the three shapes podcast clients read.

**Where clients actually look, which is not always where the spec says:**

* **Show notes** go in ``<description>`` and, as markup, in ``<content:encoded>``. Apple
  reads ``<description>``; most third-party clients prefer ``content:encoded`` when it
  exists. Both are emitted because a client that finds only the plain one shows plain text
  and a client that finds only the markup one shows raw tags.
* **Chapters** are emitted **twice**: inline as Podlove Simple Chapters (``psc:chapters``)
  and by reference as Podcasting 2.0 (``podcast:chapters``, a JSON document at a URL).
  Overcast and Podcast Addict read PSC; Fountain and the newer clients read the 2.0 tag.
  The inline form also means chapters work for a client that will not make a second
  authenticated request.
* **Subtitles** are WebVTT, referenced by ``podcast:transcript`` with ``rel="captions"``.
  VTT rather than SRT because that is what the namespace's own examples use and what a
  browser's ``<track>`` accepts unmodified.

**Timing comes from the claims**, which the TTS stage fills in by apportioning each
segment's measured duration. A caption cue is therefore a *claim* — one spoken sentence
beside one source span — which is exactly the granularity a listener wants when they stop
and ask "wait, who said that?".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from motet_db import StoredEpisode, StoredSegment

#: Longest a single caption cue may be, in characters. Past this a cue overflows a
#: lockscreen and a car display; the cue is split rather than truncated, because a
#: truncated caption is a claim with its qualifier cut off.
MAX_CUE_CHARS = 160


@dataclass(frozen=True)
class Chapter:
    """One story, as a chapter marker."""

    start_ms: int
    title: str


def chapters(episode: StoredEpisode, titles: dict[str, str]) -> list[Chapter]:
    """One chapter per story, at the offset that story starts.

    Segments with no duration are skipped: they have not been rendered, so their offsets
    are estimates rather than positions, and a chapter that jumps to the wrong place is
    worse than a missing one.
    """
    return [
        Chapter(
            start_ms=segment.start_ms,
            title=titles.get(segment.news_item_id) or f"Story {index + 1}",
        )
        for index, segment in enumerate(episode.segments)
        if segment.duration_ms > 0
    ]


def chapters_json(episode: StoredEpisode, titles: dict[str, str]) -> str:
    """The Podcasting 2.0 chapters document.

    ``version`` is the spec's string ``"1.2.0"`` and ``startTime`` is in **seconds**, as a
    number — the one place in this module where the unit changes, and the one thing a
    client silently mis-renders if it is wrong.
    """
    document: dict[str, Any] = {
        "version": "1.2.0",
        "chapters": [
            {"startTime": round(chapter.start_ms / 1000, 3), "title": chapter.title}
            for chapter in chapters(episode, titles)
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2)


def transcript_vtt(episode: StoredEpisode, titles: dict[str, str]) -> str:
    """WebVTT captions: one cue per claim, plus a chapter-ish note per story.

    The header is exactly ``WEBVTT`` followed by a blank line. A byte-order mark or a
    missing blank line makes some parsers reject the whole file, and the failure mode is a
    transcript button that does nothing.

    A cue whose text is longer than :data:`MAX_CUE_CHARS` is split across several cues that
    share the claim's time range, proportioned by length — the same apportionment the TTS
    stage uses within a segment, and correct for the same reason.
    """
    lines = ["WEBVTT", ""]
    for segment in episode.segments:
        if segment.duration_ms <= 0:
            continue
        title = titles.get(segment.news_item_id)
        if title:
            # A NOTE is a comment: every parser accepts it and none display it as a cue, so
            # a human reading the file sees the story boundaries without a listener seeing
            # a caption that is not spoken.
            lines.append(f"NOTE {title}")
            lines.append("")
        for cue_start, cue_end, text in _cues(segment):
            lines.append(f"{_timestamp(cue_start)} --> {_timestamp(cue_end)}")
            lines.append(text)
            lines.append("")
    return "\n".join(lines)


def _cues(segment: StoredSegment) -> list[tuple[int, int, str]]:
    """``(start_ms, end_ms, text)`` for one segment's claims, split to a readable length."""
    cues: list[tuple[int, int, str]] = []
    for claim in segment.claims:
        if claim.duration_ms <= 0:
            # TTS has not run, so there is no position to caption. Emitting a zero-length
            # cue would produce a caption that flashes and vanishes.
            continue
        text = " ".join(claim.text.split())
        if not text:
            continue
        for start, end, chunk in _split_cue(claim.start_ms, claim.duration_ms, text):
            cues.append((start, end, chunk))
    return cues


def _split_cue(start_ms: int, duration_ms: int, text: str) -> list[tuple[int, int, str]]:
    """Break one claim into cues no longer than :data:`MAX_CUE_CHARS`, on word boundaries."""
    if len(text) <= MAX_CUE_CHARS:
        return [(start_ms, start_ms + duration_ms, text)]

    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > MAX_CUE_CHARS:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)

    total = sum(len(chunk) for chunk in chunks) or 1
    out: list[tuple[int, int, str]] = []
    offset = 0
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            # The last chunk absorbs the rounding, so the cues of a claim end exactly
            # where the claim does.
            span = max(0, duration_ms - offset)
        else:
            span = round(duration_ms * len(chunk) / total)
        out.append((start_ms + offset, start_ms + offset + span, chunk))
        offset += span
    return out


def _timestamp(milliseconds: int) -> str:
    """``HH:MM:SS.mmm`` — WebVTT's format, with the hours field always present.

    Hours are optional in the spec and mandatory here: the two-field form is the one
    parsers disagree about, and an episode over an hour long is the case where getting it
    wrong matters.
    """
    milliseconds = max(0, milliseconds)
    seconds, millis = divmod(milliseconds, 1000)
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}.{millis:03d}"


def show_notes_text(episode: StoredEpisode, titles: dict[str, str]) -> str:
    """Plain-text show notes: what is in this episode, in the order it is spoken.

    Plain rather than markup, because this goes in ``<description>``, which is what a
    lockscreen renders — and a lockscreen either strips tags or shows them raw depending on
    the client. Timestamps are included because a listener scanning a description wants to
    know where a story is, and a plain-text timestamp is the one universal way to say it.
    """
    if not episode.segments:
        return "No stories in this episode."

    lines: list[str] = []
    for index, segment in enumerate(episode.segments):
        title = titles.get(segment.news_item_id) or f"Story {index + 1}"
        stamp = _clock(segment.start_ms) if segment.duration_ms > 0 else None
        lines.append(f"{index + 1}. {title}" + (f" ({stamp})" if stamp else ""))
    lines.append("")
    lines.append(
        "Every claim in this episode is traceable to the source it came from. "
        "The full transcript, with sources, is in the app."
    )
    return "\n".join(lines)


def show_notes_html(episode: StoredEpisode, titles: dict[str, str]) -> str:
    """Show notes as markup, for ``<content:encoded>``.

    Deliberately austere — an ordered list and a closing line. Podcast clients render show
    notes in a webview with their own stylesheet, so anything beyond structure is either
    ignored or fights the client's theme.

    Escaping is done here rather than left to the XML writer because this string is placed
    inside a CDATA section: the whole point of ``content:encoded`` is that its markup
    survives, so the writer must not escape it and *this* must.
    """
    if not episode.segments:
        return "<p>No stories in this episode.</p>"

    items = []
    for index, segment in enumerate(episode.segments):
        title = _escape(titles.get(segment.news_item_id) or f"Story {index + 1}")
        stamp = f" <em>({_clock(segment.start_ms)})</em>" if segment.duration_ms > 0 else ""
        items.append(f"<li>{title}{stamp}</li>")
    return (
        "<ol>" + "".join(items) + "</ol>"
        "<p>Every claim in this episode is traceable to the source it came from.</p>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        # `]]>` would close the CDATA section this ends up inside, which is the one way a
        # newsletter title can break a feed document that escaping the angle brackets does
        # not already cover.
        .replace("]]>", "]]&gt;")
    )


def _clock(milliseconds: int) -> str:
    """``M:SS`` or ``H:MM:SS`` — how a human writes a timestamp in show notes."""
    total = max(0, milliseconds) // 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def segment_titles(segments: Sequence[StoredSegment], titles: dict[str, str]) -> list[str]:
    """Story titles in spoken order, falling back to a positional name."""
    return [
        titles.get(segment.news_item_id) or f"Story {index + 1}"
        for index, segment in enumerate(segments)
    ]
