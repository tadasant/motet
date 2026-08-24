"""What the three text stages actually say to a model, and how they read the answer back.

Kept apart from ``adapters.py`` so that the adapters stay readable as *wiring* — build a
request, send it, parse it, return a value type — and so that a prompt can be diffed
without a class definition around it.

**The load-bearing decision in this file is that the model never emits a character
offset.** It emits a `quote`: a run of text it asserts appears verbatim in a named source
item. The adapter then *locates* that quote and derives the span itself. Two things fall
out of that, and both are why invariant 3 is enforceable at all:

* Models are unreliable at counting characters and reliable at copying text. Asking for
  offsets produces spans that are plausible and off by nine, which is worse than useless
  — it is a citation that points at the wrong sentence.
* A quote that cannot be found verbatim is *detected*, not trusted. The adapter drops the
  claim rather than inventing a span for it, so a fabricated quotation cannot become a
  grounded-looking claim.

The spoken text and the evidence are therefore separate fields: ``text`` is narration and
may paraphrase, ``quote`` is the verbatim thing it is answerable to. Grounding validation
judges the first against the second.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .llm import CacheControl, JsonSchemaFormat, LlmResponse, Message, TextPart
from .types import NewsItem, SourceItem

# --- dedup / integrate ---------------------------------------------------------------

INTEGRATE_SYSTEM = """\
You are the deduplication stage of a personal news briefing pipeline.

You receive a WINDOW of news items the reader already has, and ONE new source item just
ingested. Decide whether the new source item is *the same story* as one of the existing
news items, or a new story.

Same story means the same underlying event or announcement, even when the wording,
framing, and detail differ — two newsletters covering one funding round are one story.
Different stories about the same company are NOT the same story. A follow-up that reports
genuinely new developments is NOT the same story as the original announcement.

Return:
- decision "merge" with the id of the existing news item, when it is the same story. Give
  an updated title and summary that reflect what BOTH sources now say.
- decision "new" with news_item_id null, when it is a new story. Give a title and summary
  for it.

Titles are a short headline, under 100 characters, no trailing punctuation. Summaries are
one or two sentences describing what happened. Both are read by a human skimming a
backlog, and the summary is also what a duration estimate is made from — so keep it tight
and factual. Never invent detail that is not in the sources."""

INTEGRATE_SCHEMA = JsonSchemaFormat(
    name="integration_decision",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "news_item_id", "title", "summary"],
        "properties": {
            "decision": {"type": "string", "enum": ["merge", "new"]},
            "news_item_id": {
                "type": ["string", "null"],
                "description": "Id of the existing news item, when decision is 'merge'.",
            },
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
    },
)


def render_window(window: Sequence[NewsItem]) -> str:
    """The existing stories, as the stable prefix of the dedup prompt.

    Stable is the operative word: this rendering is byte-identical across every source
    item integrated in one ingestion run, which is what makes the cache breakpoint that
    follows it worth having. Anything volatile in here — a timestamp, a counter, the
    item being integrated — would silently cost a cache miss per call, on the highest
    volume stage in the system.
    """
    if not window:
        return "EXISTING NEWS ITEMS: (none — the backlog is empty)"
    lines = ["EXISTING NEWS ITEMS:"]
    for item in window:
        lines.append(f"- id: {item.id}\n  title: {item.title}\n  summary: {item.summary}")
    return "\n".join(lines)


def render_source_item(item: SourceItem) -> str:
    return f"NEW SOURCE ITEM:\nid: {item.id}\ntitle: {item.title}\n\n{item.text}"


def integrate_messages(item: SourceItem, window: Sequence[NewsItem]) -> tuple[Message, ...]:
    """System, then window, then the new item — breakpoints on the two stable parts.

    The window and the source item are two *parts of one user message* rather than two
    messages, because a cache breakpoint sits between parts and the boundary has to fall
    exactly where "stable" stops.
    """
    return (
        Message.of("system", INTEGRATE_SYSTEM, cache=CacheControl(ttl="1h")),
        Message(
            role="user",
            parts=(
                TextPart(text=render_window(window) + "\n\n", cache=CacheControl(ttl="1h")),
                TextPart(text=render_source_item(item)),
            ),
        ),
    )


# --- script generation ---------------------------------------------------------------

SCRIPT_SYSTEM = """\
You write the script for a personal audio briefing. It is read aloud by a text-to-speech
voice on a dog walk, so it must sound like a person talking: full sentences, no bullet
points, no headings, no markdown, no "in this segment we will".

You receive news items, each with the full text of the sources behind it. Write one
segment per news item, in the order given.

A segment is a list of CLAIMS. Each claim has:
- "text": what gets spoken. One or two sentences of natural narration. It may paraphrase.
- "quote": a span of text copied EXACTLY, character for character, from one of that news
  item's sources. It is the evidence for what you just said.
- "source_item_id": which source the quote was copied from.

The quote is checked by an exact string search against the source. If it does not match
character for character — different quotation marks, a fixed typo, an ellipsis, joined
line breaks — the claim is DISCARDED and the listener never hears it. Copy, do not
retype. Prefer a quote of one full sentence.

Every factual assertion you speak must be covered by the quote attached to it. Do not
state a number, a name, or a date that its quote does not contain. If a source does not
support something, leave it out — an omission is fine, an invention ends the product.

Two to four claims per segment, and no greeting or sign-off. Every word you write has to
be covered by the quote attached to it, and "good morning" is not in anybody's newsletter
— an opening pleasantry would be dropped by the grounding gate along with the claim
carrying it, which would cost the lead story its first sentence."""

SCRIPT_SCHEMA = JsonSchemaFormat(
    name="briefing_script",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["segments"],
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["news_item_id", "claims"],
                    "properties": {
                        "news_item_id": {"type": "string"},
                        "claims": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["text", "quote", "source_item_id"],
                                "properties": {
                                    "text": {"type": "string"},
                                    "quote": {"type": "string"},
                                    "source_item_id": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            }
        },
    },
)


def script_messages(
    news_items: Sequence[NewsItem], sources: Mapping[str, SourceItem]
) -> tuple[Message, ...]:
    blocks: list[str] = []
    for item in news_items:
        lines = [f"NEWS ITEM {item.id}", f"title: {item.title}", f"summary: {item.summary}", ""]
        for source_id in item.source_item_ids:
            source = sources.get(source_id)
            if source is None:
                continue
            lines.append(f"--- SOURCE {source_id} ({source.title}) ---")
            lines.append(source.text)
            lines.append("")
        blocks.append("\n".join(lines))
    return (
        Message.of("system", SCRIPT_SYSTEM, cache=CacheControl()),
        Message.of("user", "\n\n".join(blocks)),
    )


# --- grounding validation ------------------------------------------------------------

GROUNDING_SYSTEM = """\
You are the grounding gate of a news briefing pipeline. Nothing you reject is spoken.

You receive numbered claims. Each has SPOKEN text and the EVIDENCE it cites — a verbatim
span already confirmed to exist in the source. Your only job is to judge whether the
evidence supports the spoken text.

Supported means: a careful reader of the evidence alone would agree the spoken sentence is
true and not misleading. Paraphrase is fine. Compression is fine. Reasonable rewording for
speech is fine.

NOT supported, and these are the failures that matter:
- a number, name, date, or quantity in the spoken text that the evidence does not state
- a causal or comparative claim ("because", "the largest", "the first") the evidence does
  not make
- an inference about consequences or intent that the evidence does not state
- a hedge in the evidence ("reportedly", "expects to") dropped in the spoken text

Be strict. A false positive here is a fabricated fact reaching a listener's ears, which is
the failure this whole system is built to prevent. When genuinely uncertain, mark it
unsupported and say why in one short sentence."""

GROUNDING_SCHEMA = JsonSchemaFormat(
    name="grounding_verdicts",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["verdicts"],
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "supported", "reason"],
                    "properties": {
                        "index": {"type": "integer"},
                        "supported": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
)


def grounding_messages(claims: Sequence[tuple[int, str, str]]) -> tuple[Message, ...]:
    """``claims`` is ``(index, spoken_text, evidence_text)``, already span-resolved."""
    blocks = [
        f"CLAIM {index}\nSPOKEN: {spoken}\nEVIDENCE: {evidence}"
        for index, spoken, evidence in claims
    ]
    return (
        Message.of("system", GROUNDING_SYSTEM, cache=CacheControl()),
        Message.of("user", "\n\n".join(blocks)),
    )


# --- parsing -------------------------------------------------------------------------


class PromptResponseError(ValueError):
    """The model answered with something the schema said it could not.

    Raised rather than defaulted, because every field these stages read is load-bearing:
    a missing ``decision`` is not "probably new", and a missing verdict is not "probably
    supported".
    """


def parse_json_object(response: LlmResponse, *, what: str) -> dict[str, Any]:
    """Read a JSON object out of a response, with an error that says what was expected."""
    try:
        parsed = json.loads(response.text)
    except json.JSONDecodeError as exc:
        preview = response.text[:300]
        raise PromptResponseError(f"{what}: response was not JSON ({exc}): {preview!r}") from exc
    if not isinstance(parsed, dict):
        raise PromptResponseError(f"{what}: expected a JSON object, got {type(parsed).__name__}")
    return parsed


def require_str(obj: Mapping[str, Any], key: str, *, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise PromptResponseError(f"{what}: {key!r} must be a string, got {value!r}")
    return value


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Find ``quote`` in ``text`` and return its half-open span, or ``None``.

    Exact match first. Failing that, one retry that treats any run of whitespace in the
    quote as matching any run of whitespace in the source — a model reliably copies words
    and unreliably copies line breaks, and a newsletter wrapped at 80 columns comes back
    as one line more often than not. Allowing only that difference keeps the resulting
    span verbatim in every respect that carries meaning.

    Returning ``None`` rather than a best guess is the whole point: a quote that cannot
    be found is a quote the model did not copy, and a claim whose evidence cannot be
    located must be discarded rather than given a plausible-looking span. That is what
    stops a fabricated quotation from becoming a grounded-looking claim.
    """
    stripped = quote.strip()
    if not stripped:
        return None

    exact = text.find(stripped)
    if exact != -1:
        return exact, exact + len(stripped)

    # Built by escaping each word and joining with `\s+`, rather than by substituting
    # into an already-escaped string: `re.escape` escapes spaces on some versions and not
    # others, so a substitution over its output silently produces a different pattern
    # depending on the interpreter. Splitting first sidesteps the question entirely.
    pattern = r"\s+".join(re.escape(token) for token in stripped.split())
    match = re.search(pattern, text)
    if match is None:
        return None
    return match.start(), match.end()
