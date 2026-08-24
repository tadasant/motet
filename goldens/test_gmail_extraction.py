"""The Gmail extraction corpus.

Newsletters in, clean text out — the regression test for the part of Gmail ingestion that
has no single right answer and fails *quietly*. A broken extractor does not crash; it
produces a briefing that opens with a preheader nobody wrote, or reads an unsubscribe
footer aloud, or drops a story that a claim then cannot cite.

Each case is a complete RFC 822 message, so a fixture is exactly what Gmail would hand
back. Assertions are on **content**, not on an exact string: pinning the whole extracted
body would break on every whitespace tweak and would say nothing about whether the
extraction was right. What the cases pin is the pair of properties that matter — the prose
survived, and the machinery did not.

Runs against no vendor and no network: extraction is pure, which is exactly why it can be
the most heavily covered part of this path before a single credential exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from motet_sources import ExtractionError, extract_newsletter

CASES_DIR = Path(__file__).resolve().parent / "gmail"


@dataclass(frozen=True)
class GmailCase:
    name: str
    why: str
    raw: bytes
    refused: bool
    title: str | None
    contains: tuple[str, ...]
    excludes: tuple[str, ...]
    no_c1_controls: bool

    def __str__(self) -> str:
        return self.name


def load_cases() -> list[GmailCase]:
    cases: list[GmailCase] = []
    for directory in sorted(CASES_DIR.iterdir()):
        if not directory.is_dir():
            continue
        spec = json.loads((directory / "expected.json").read_text())
        cases.append(
            GmailCase(
                name=directory.name,
                why=spec["why"],
                raw=(directory / "message.eml").read_bytes(),
                refused=bool(spec.get("refused")),
                title=spec.get("title"),
                contains=tuple(spec.get("text_contains", ())),
                excludes=tuple(spec.get("text_excludes", ())),
                no_c1_controls=bool(spec.get("no_c1_controls")),
            )
        )
    return cases


CASES = load_cases()
EXTRACTED = [case for case in CASES if not case.refused]
REFUSED = [case for case in CASES if case.refused]


def test_the_corpus_is_not_empty() -> None:
    """Guards against a fixture path change silently reducing this file to a no-op."""
    assert CASES
    assert EXTRACTED, "every case being a refusal would prove nothing about extraction"
    assert REFUSED, "a corpus with no refusals does not cover the not-a-newsletter path"


@pytest.mark.parametrize("case", EXTRACTED, ids=str)
def test_the_title_is_what_the_case_expects(case: GmailCase) -> None:
    """The title is the source item's title, so it is what dedup compares first.

    It is also read aloud and put in an RSS document, which is why the encoding cases pin
    it exactly rather than approximately.
    """
    assert case.title is not None
    assert extract_newsletter(case.raw).title == case.title, case.why


@pytest.mark.parametrize("case", EXTRACTED, ids=str)
def test_the_prose_survives(case: GmailCase) -> None:
    """Every sentence the case says is content must be in the output.

    Trimming boilerplate off the ends is safe; losing a sentence from the middle makes a
    claim uncitable, and the grounding validator would then reject the story rather than
    tell you extraction ate it.
    """
    text = extract_newsletter(case.raw).text
    for fragment in case.contains:
        assert fragment in text, f"{case.why}\n\nmissing: {fragment!r}\n\ngot:\n{text}"


@pytest.mark.parametrize("case", EXTRACTED, ids=str)
def test_the_machinery_does_not(case: GmailCase) -> None:
    """Preheaders, nav bars, tracking URLs and footers must not reach the briefing."""
    text = extract_newsletter(case.raw).text
    for fragment in case.excludes:
        assert fragment.lower() not in text.lower(), (
            f"{case.why}\n\nleaked: {fragment!r}\n\ngot:\n{text}"
        )


@pytest.mark.parametrize("case", EXTRACTED, ids=str)
def test_no_invisible_or_control_characters_survive(case: GmailCase) -> None:
    """Nothing unspeakable reaches TTS, and nothing invisible reaches a span.

    C1 controls come from cp1252 mislabelled as latin-1; zero-width characters are how a
    sender fingerprints a send. Both would sit inside a span a claim cites, so a highlight
    would quote characters nobody can see.
    """
    item = extract_newsletter(case.raw)
    for field, value in (("title", item.title), ("text", item.text)):
        offenders = [
            (index, hex(ord(ch)))
            for index, ch in enumerate(value)
            if "\u0080" <= ch <= "\u009f" or ch in "\u200b\u200c\u200d\u200e\u200f\ufeff"
        ]
        assert not offenders, f"{case.name} {field} carries {offenders[:5]}"


@pytest.mark.parametrize("case", REFUSED, ids=str)
def test_a_non_newsletter_is_refused(case: GmailCase) -> None:
    """Refusal is a *permanent* outcome, not a retryable error.

    A mailbox is mostly receipts and notifications. Treating each one as a failure would
    make the source permanently red and retry it five times.
    """
    with pytest.raises(ExtractionError):
        extract_newsletter(case.raw)


@pytest.mark.parametrize("case", CASES, ids=str)
def test_extraction_is_deterministic(case: GmailCase) -> None:
    """Same bytes, same characters.

    Without this the corpus cannot be a regression test, and — more importantly — every
    span into the extracted text would be a lottery.
    """
    try:
        first = extract_newsletter(case.raw)
    except ExtractionError as exc:
        with pytest.raises(ExtractionError, match=str(exc)[:20]):
            extract_newsletter(case.raw)
        return
    assert extract_newsletter(case.raw) == first


@pytest.mark.parametrize("case", EXTRACTED, ids=str)
def test_the_extracted_text_can_anchor_a_span(case: GmailCase) -> None:
    """The output has to work as the immutable anchor invariant 3 needs.

    Every fragment the case pins is located and re-read from the same offsets, which is the
    operation a claim and a highlight both perform.
    """
    text = extract_newsletter(case.raw).text
    for fragment in case.contains:
        start = text.index(fragment)
        end = start + len(fragment)
        assert text[start:end] == fragment
        assert 0 <= start < end <= len(text)
