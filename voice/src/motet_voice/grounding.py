"""The grounding check on the *conversational* reply path — invariant 3, deliberately scoped.

**Read the scope before you change anything here.**

Invariant 3 says every reported claim carries a source span, validated before TTS. On the
**narration** path that is a hard pipeline gate — script → grounding validation → Cartesia —
and nothing is synthesized until the report passes. That has not changed and must not: a
briefing that invents a funding number is dead, and the batch path is where a briefing is
made.

This module is the **conversational** path's half, and it is advisory by decision (Tadas,
2026-08-24, motet#10). Three properties, and the third is the one that keeps "advisory"
from collapsing into "absent":

1. **The check still happens**, on every conversational reply the arm produces.
2. **It does not gate the audio.** A conversational reply is generated inside a spoken turn
   with a listener standing on a pavement waiting for an answer, and a gate there is a
   silence. The check runs *after* the reply has been handed to the client — see
   :meth:`~motet_voice.session.VoiceSession.respond_to_text`.
3. **The verdict is recorded**, every time: a counter on the obs stack (which is what an
   operator queries in Grafana), a warning in the log with the offending specifics, a
   ``grounding`` event to the client, and a count in the session summary. "How often does
   Motet say something it cannot source out loud?" is therefore a question with an answer
   rather than a shrug.

**The checker is ours — local, deterministic, free.** Like the VAD and unlike every
inference stage: no vendor call, no credential, no latency budget, and no fake/real split,
so it computes the same verdict in CI, on a laptop and in production. That is a decision
rather than a stopgap. A check that is itself a model call would be dormant exactly when
the arm is dormant, would cost a second inference per spoken turn, and would be untestable
without a key — and a check that silently no-ops on a missing credential is the failure
this module exists to prevent, not a smaller version of it.

**What it can and cannot see.** It catches *fabricated specifics*: a number, a name or a
quotation in the reply that does not appear in the material the session was given. That is
invariant 3's own named failure mode arriving through the interactive surface. It does
**not** judge paraphrase or entailment — a reply that draws a wrong conclusion from
material it does quote reads as grounded here. A model-backed entailment check is the
obvious upgrade and drops in behind :class:`ConversationGroundingChecker` without touching
a caller; it is not here because it is a second inference call per turn and the failure
above is the one worth catching first.

False positives are the tolerated direction. ``$4,200,000`` against material that says
"$4.2 million" reads as unsupported. The verdict is advisory and each one carries the
offending text, so an operator can tell a fabrication from a reformatting; a checker tuned
the other way would report clean and mean nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, runtime_checkable

#: How a specific failed. Carried per item rather than summarized, because "a number the
#: material does not contain" and "a name nobody mentioned" are different bugs.
UnsupportedKind = Literal["number", "name", "quote"]

#: Anything with a digit in it, commas and decimal points included: ``4.2``, ``1,200``,
#: ``2026``. Currency symbols and units are stripped by the tokenizer around it, so ``$4.2
#: million`` yields ``4.2`` and is compared against the material's own ``4.2``.
_NUMBER: Final = re.compile(r"\d[\d,.]*")

#: A quotation the reply attributes to the material. Both straight and curly pairs; short
#: enough to be a stylistic aside is not worth checking, hence the length floor.
#:
#: **Deliberately unbounded above.** A ceiling here does not skip a long quotation, it
#: restarts the engine on the quotation's own closing mark — so the long quote goes
#: unchecked and the unquoted prose after it gets reported as a fabricated one. The class
#: is negated, so there is no backtracking cost to leaving it open.
_QUOTE: Final = re.compile(r"[\"“]([^\"”\n]{8,})[\"”]")

#: A word: one letter, then letters, apostrophes or hyphens. ``[^\W\d_]`` rather than
#: ``[A-Za-z]`` because a name is not always Latin-1 — with the ASCII class, "Zoë" simply
#: was not a word, so a fabricated non-ASCII name was never checked and the verdict read
#: ``checked=0``, which is what a reply that asserted nothing looks like.
_WORD: Final = re.compile(r"[^\W\d_](?:[^\W\d_]|['’\-])*")

#: Trailing clitics: the possessive, and the contracted auxiliaries.
#:
#: Stripped before a word is looked up, in the reply *and* in the material, and this is
#: not tidying. Without it ``It's`` and ``Let's`` are capitalized words no material
#: contains — so almost every reply that opens a sentence with a contraction reports
#: ungrounded — and ``Sequoia's`` fails to match the ``Sequoia`` sitting in the material.
#: Both are false positives on the one number this whole change exists to make meaningful.
_CLITICS: Final = (
    "'s", "’s", "'t", "’t", "'re", "’re", "'ve", "’ve", "'ll", "’ll", "'d", "’d", "'m", "’m",
)  # fmt: skip

#: Capitalized words that are not names, in any position.
#:
#: **A sentence-initial capital is checked like any other**, which is the choice that costs
#: something and is still right: a model writes "Sequoia led that round", and skipping the
#: first word of every sentence would let a fabricated name through whenever it happened to
#: land there — the single most likely place for the subject of a sentence to be.
#:
#: The price is this list, and it is bounded on purpose: the common words a *conversational
#: reply* actually opens with, plus the function words that run through the middle of one.
#: Not an English dictionary — there is no attempt to cover the language, only the words
#: that get capitalized for grammar rather than because they name something. A false
#: positive from a word missing here is a line in a log an operator can dismiss; a
#: fabricated name that is never counted is the thing motet#10 exists to stop.
#:
#: **Months and weekdays are deliberately absent.** An invented date is a fabrication of
#: exactly the kind worth catching, so "Tuesday" is checked against the material like any
#: other name.
_NOT_A_NAME: Final = frozenset(
    {
        "a", "about", "actually", "after", "again", "all", "also", "always", "am", "an",
        "and", "another", "any", "anything", "are", "as", "ask", "at", "back", "basically",
        "be", "because", "been", "before", "being", "besides", "best", "both", "but", "by",
        "call", "can", "come", "could", "did", "do", "does", "doing", "done", "down",
        "each", "either", "else", "enough", "even", "ever", "every", "everything",
        "exactly", "few", "find", "first", "for", "from", "get", "give", "going", "good",
        "got", "great", "had", "half", "has", "have", "he", "her", "here", "hers", "him",
        "his", "hold", "honestly", "how", "however", "i", "if", "in", "into", "is", "it",
        "its", "just", "keep", "know", "last", "later", "least", "less", "let", "like",
        "little", "long", "look", "lot", "made", "make", "many", "may", "maybe", "me",
        "might", "mine", "more", "most", "much", "must", "my", "near", "need", "never",
        "new", "next", "nice", "no", "none", "nope", "nor", "not", "nothing", "now", "of",
        "off", "okay", "old", "on", "once", "one", "only", "or", "other", "others", "our",
        "out", "over", "own", "part", "per", "perhaps", "please", "point", "probably",
        "put", "quick", "quite", "rather", "really", "right", "said", "same", "say", "see",
        "seems", "several", "shall", "she", "short", "should", "since", "small", "so",
        "some", "something", "sorry", "sort", "sounds", "still", "such", "sure", "take",
        "tell", "than", "thanks", "that", "the", "their", "theirs", "them", "then",
        "there", "therefore", "these", "they", "thing", "things", "think", "this", "those",
        "though", "three", "through", "time", "to", "together", "too", "two", "under",
        "until", "up", "us", "use", "used", "very", "want", "was", "way", "we", "well",
        "were", "what", "when", "where", "whether", "which", "while", "who", "whole",
        "whose", "why", "will", "with", "within", "without", "would", "yeah", "yes", "yet",
        "you", "your", "yours",
    }
)  # fmt: skip

#: Below this length a capitalized token is an initial, an acronym too short to be worth
#: checking, or a stray. Above it, an unknown capitalized word is a name the reply made up.
_MIN_NAME_LENGTH: Final = 3


@dataclass(frozen=True)
class Unsupported:
    """One specific in the reply that the material does not contain."""

    kind: UnsupportedKind
    text: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class GroundingVerdict:
    """The advisory verdict on one conversational reply.

    ``grounded`` is *not* invariant 3's ``GroundingReport.ok``, and the two types are
    deliberately different so that nothing can pass one where the other is expected. That
    one gates TTS on the narration path; this one gates nothing.
    """

    checker: str
    #: How many specifics were examined. ``0`` with ``grounded`` true means the reply
    #: asserted nothing checkable — a refusal, or "I'll look that up" — which is the
    #: behaviour the system prompt asks for and not a check that failed to run.
    checked: int = 0
    unsupported: tuple[Unsupported, ...] = field(default=())

    @property
    def grounded(self) -> bool:
        return not self.unsupported

    def to_json(self) -> dict[str, Any]:
        return {
            "checker": self.checker,
            "grounded": self.grounded,
            "checked": self.checked,
            "unsupported": [item.to_json() for item in self.unsupported],
        }

    def summarize(self) -> str:
        """One line for the log, listing what could not be sourced."""
        if self.grounded:
            return f"grounded ({self.checked} specific(s) checked by {self.checker})"
        listed = ", ".join(f"{item.kind}={item.text!r}" for item in self.unsupported)
        return f"{len(self.unsupported)} of {self.checked} specific(s) unsupported: {listed}"


@runtime_checkable
class ConversationGroundingChecker(Protocol):
    """Is this reply supported by the material the session was given?

    Synchronous and side-effect free on purpose: the caller decides when it runs (off the
    turn, in a worker thread) and what to do with the verdict (record it, never block on
    it). A checker that awaited would invite somebody to await it inside the turn.
    """

    @property
    def name(self) -> str: ...

    def check(self, reply: str, material: str) -> GroundingVerdict: ...


@dataclass(frozen=True)
class SpecificsGroundingChecker:
    """Every number, name and quotation in the reply must appear in the material.

    The whole check, and it is worth stating why the comparison is this blunt: the material
    is prose the caller assembled from narration that was *already* grounded on the batch
    path, so a number in the reply either came from that prose or the model produced it
    from somewhere invariant 3 cannot see. Set membership answers exactly that question and
    nothing else, which is what makes the verdict interpretable at three in the morning.
    """

    @property
    def name(self) -> str:
        return "specifics"

    def check(self, reply: str, material: str) -> GroundingVerdict:
        numbers = set(_numbers(material))
        words = {_stem(word).lower() for word in _WORD.findall(material)}
        haystack = _flatten(material)

        unsupported: list[Unsupported] = []
        checked = 0

        for number in _numbers(reply):
            checked += 1
            if number not in numbers:
                unsupported.append(Unsupported(kind="number", text=number))

        for name in _names(reply):
            checked += 1
            if name.lower() not in words:
                unsupported.append(Unsupported(kind="name", text=name))

        for quote in _QUOTE.findall(reply):
            checked += 1
            if _flatten(quote) not in haystack:
                unsupported.append(Unsupported(kind="quote", text=quote.strip()))

        return GroundingVerdict(
            checker=self.name, checked=checked, unsupported=tuple(_dedupe(unsupported))
        )


def build_grounding_checker() -> ConversationGroundingChecker:
    """The checker this process uses.

    A function rather than a constant, and deliberately with no environment variable behind
    it. There is no switch to turn the check off: it is already advisory, it costs a
    fraction of a millisecond and no money, and a disabled advisory check is indis-
    tinguishable on the obs stack from a service that is simply never wrong.
    """
    return SpecificsGroundingChecker()


def material_for(*, context_notes: str, user_text: str, tool_results: Sequence[str] = ()) -> str:
    """Everything a reply is allowed to have drawn on, as one blob.

    Three sources, and the omission is the interesting part:

    * ``context_notes`` — what the caller that owns the database put in the session config.
      Assembled from narration that already passed invariant 3's gate.
    * ``user_text`` — the listener's own turn. A number the listener said and the assistant
      repeated back is grounded in the conversation, and flagging it would bury the real
      signal under echoes.
    * ``tool_results`` — what a tool returned during this turn. ``get_item_detail`` hands
      back a news item's spans, which is precisely the material a grounded answer is meant
      to reach for; not counting it would punish the behaviour the prompt asks for.

    **Prior assistant turns are excluded, and that is the load-bearing omission.** Folding
    them in would let an ungrounded claim from turn three become the material that grounds
    the same claim on turn four — a check that launders its own misses and reports clean
    for the rest of the walk.
    """
    return "\n\n".join(part for part in (context_notes, user_text, *tool_results) if part.strip())


def _numbers(text: str) -> Iterator[str]:
    """Digit-bearing tokens, normalized so ``1,200`` and ``1200`` are the same number."""
    for raw in _NUMBER.findall(text):
        normalized = raw.replace(",", "").rstrip(".")
        if normalized:
            yield normalized


def _names(text: str) -> Iterator[str]:
    """Capitalized words that are capitalized because they name something.

    Position in the sentence is deliberately not consulted — see :data:`_NOT_A_NAME` for
    why skipping the first word would be the wrong trade. The *stem* is yielded rather
    than the word as written, because the stem is what was actually looked up.
    """
    for word in _WORD.findall(text):
        stem = _stem(word)
        if len(stem) < _MIN_NAME_LENGTH or not stem[0].isupper():
            continue
        if stem.lower() in _NOT_A_NAME:
            continue
        yield stem


def _stem(word: str) -> str:
    """Drop a trailing clitic, so ``Sequoia's`` and ``Sequoia`` are one word."""
    lowered = word.lower()
    for clitic in _CLITICS:
        if len(word) > len(clitic) and lowered.endswith(clitic):
            return word[: -len(clitic)]
    return word


def _flatten(text: str) -> str:
    """Lowercase with runs of whitespace collapsed, so a quote survives a line wrap."""
    return " ".join(text.lower().split())


def _dedupe(items: Iterable[Unsupported]) -> Iterator[Unsupported]:
    """One entry per distinct specific: a name repeated four times is one fabrication."""
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.kind, item.text.lower())
        if key not in seen:
            seen.add(key)
            yield item
