"""The platform tools, and how each one becomes a call on Motet's MCP server.

```
mark_read      save_highlight
```

**Every one of them is an MCP ``tools/call`` against Motet's own API** (motet#120), never a
query. That is invariant 3, and it is also what makes these tools work for Zimmer later: a
caller with a different backend binds a different slug and the tools are unchanged.

**What used to be here was a table of HTTP path templates, kept by hand, and three of the
four were wrong.** ``get_item_detail`` named ``GET /v1/news-items/{id}`` and
``start_research`` named ``/v1/research``; neither route exists. ``save_highlight`` named a
route that *does* exist and posted a body it does not accept — ``quote``, where
``POST /v1/highlights`` wants the source span to read the quote out of. Only ``mark_read``
worked, which is why ``motet_api.voice`` granted a session that one tool and nothing else.
A template cannot be wrong now: the name is a tool on a server whose parity with the route
table is a test (``api/tests/test_mcp_parity.py``), and a name that server does not know is
refused by it.

So two tools are gone rather than dormant:

* **``start_research``** needed Exa, which is not provisioned, *and* a route nobody has
  designed. Inventing one would be new architecture (invariant 12), not a transport change.
* **``get_item_detail``** needed a single-news-item read that does not exist. Adding it is a
  route, a repository query, a regenerated OpenAPI document and client, and a registry
  entry — for detail a session has already been handed: ``SessionContext.transcript``
  carries every story, every claim and (since motet#120) the source span behind it, and
  ``notes`` carries the backdrop. Dropping it is the smaller honest change; adding the
  route stays available to anyone who finds the session genuinely short of something.

**A highlight's span is resolved here, from the session's own transcript, and never taken
from the model.** ``POST /v1/highlights`` reads the quote out of the source text at the span
it is given — AGENTS.md, "a highlight's quote is read out of the source item, never taken
from the caller" — so something has to turn "save that bit" into a span. The model names
the line it heard; this module matches it against the claims the session was given and
sends *that claim's* span. A model that paraphrases loosely therefore fails to save rather
than saving its own words, and a model cannot invent an offset because it is never asked
for one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from ..config import MOTET_MCP_SLUG, VoiceSettings
from ..contract import SessionContext, TimedClaim
from .spec import (
    AVAILABLE,
    ToolAvailability,
    ToolResponse,
    ToolResult,
    ToolState,
    ToolTransport,
)

#: Said when a session was never bound to Motet's MCP server, or when this deployment
#: resolves no slug for it. Named after the binding rather than after a URL, because the
#: slug is the only half of it a caller ever sees.
UNBOUND = (
    f"this voice session is not bound to the {MOTET_MCP_SLUG!r} MCP server, so it cannot "
    f"reach Motet"
)


@dataclass
class McpTool:
    """A platform tool backed by one tool on a bound MCP server.

    One class rather than one per tool, because they differ only in which remote tool they
    call and how they shape its arguments — and a subclass per tool is one place per tool
    for the defaults-merging rule to be got subtly wrong.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    #: The tool's name **on the server**, which is not always the name the persona knows:
    #: ``mark_read`` is Motet's ``set_news_item_read``. The persona's names are a contract
    #: with callers (``PLATFORM_TOOLS``) and the server's are a contract with the API, and
    #: pinning them together would make either one unable to move.
    tool: str
    transport: ToolTransport | None = None
    #: Bound by the caller at StartSession — the episode a session is about, say. Merged
    #: **under** the model's arguments, never over them.
    defaults: dict[str, Any] = field(default_factory=dict)
    #: Turns the merged arguments into the server tool's arguments. Identity by default;
    #: ``save_highlight`` is the one that does real work. Returning a ``str`` is how it
    #: fails — a sentence the persona can say, never an exception.
    shape: Callable[[Mapping[str, Any]], dict[str, Any] | str] | None = None
    unavailable_reason: str = ""

    def availability(self) -> ToolAvailability:
        if self.unavailable_reason:
            return ToolAvailability(ToolState.DORMANT, self.unavailable_reason)
        if self.transport is None:
            return ToolAvailability(ToolState.DORMANT, UNBOUND)
        return AVAILABLE

    async def invoke(self, arguments: Mapping[str, Any]) -> ToolResult:
        if self.transport is None:  # pragma: no cover — the registry checks availability
            return ToolResult.failure(UNBOUND)

        merged: dict[str, Any] = {**self.defaults, **dict(arguments)}
        # Required-ness is checked against what the *model* was asked for, before shaping:
        # `save_highlight` takes a quote and sends a span, so a check on the shaped output
        # would be looking for arguments the model was never offered.
        if (missing := _missing(self.parameters, merged)) is not None:
            return ToolResult.failure(f"{self.name} needs a {missing}")
        if self.shape is not None:
            shaped = self.shape(merged)
            if isinstance(shaped, str):
                return ToolResult.failure(shaped)
            merged = shaped

        response = await self.transport.call_tool(self.tool, merged)
        if not response.ok:
            return ToolResult.failure(explain(self.name, response))
        return ToolResult(ok=True, result=response.payload)


def _missing(parameters: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """The first required argument the model did not give and no binding supplied.

    Checked here rather than left to the server, because "mark_read needs a news_item_id"
    is something the persona can act on in the same turn and ``422: …`` is not.
    """
    for name in parameters.get("required", ()):
        if arguments.get(name) in (None, ""):
            return str(name)
    return None


def explain(name: str, response: ToolResponse) -> str:
    """What went wrong, in words a persona can say out loud."""
    detail = str(response.payload.get("detail") or response.payload.get("error") or "").strip()
    if response.status == 404:
        return f"{name} got a 404 from Motet: the item does not exist, or it is not yours."
    if response.status == 599:
        return f"{name} could not reach Motet (599)" + (f": {detail}" if detail else "")
    return f"{name} failed with status {response.status}" + (f": {detail}" if detail else "")


def build_platform_tools(
    settings: VoiceSettings,
    *,
    transport: ToolTransport | None = None,
    defaults: Mapping[str, Mapping[str, Any]] | None = None,
    context: SessionContext | None = None,
) -> dict[str, McpTool]:
    """The platform tools, wired to a bound server and told what they cannot do.

    ``transport`` is ``None`` for a session that bound no MCP server: the tools still exist
    and are described to the persona as dormant, so it says "I can't do that here" rather
    than promising and failing.
    """
    del settings  # Kept in the signature: every other seam in this service is settings-led.
    bound = defaults or {}
    session = context or SessionContext()

    def _defaults(name: str) -> dict[str, Any]:
        return dict(bound.get(name, {}))

    return {
        "save_highlight": McpTool(
            name="save_highlight",
            description=(
                "Save something the listener wants to keep from the story being discussed. "
                "Use it when they say 'save that', 'remember this', or quote a line back. "
                "Pass the line as it was narrated — it is matched against what was read "
                "out, and the highlight is saved from the source text behind it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "quote": {
                        "type": "string",
                        "description": "The narrated line worth keeping, as it was said.",
                    },
                    "news_item_id": {
                        "type": "string",
                        "description": "The story it belongs to, if you know which.",
                    },
                    "note": {"type": "string", "description": "Why, in the listener's words."},
                },
                "required": ["quote"],
            },
            tool="save_highlight",
            transport=transport,
            defaults=_defaults("save_highlight"),
            shape=_highlight_shaper(session),
        ),
        "mark_read": McpTool(
            name="mark_read",
            description=(
                "Mark a news item read, or unread. Read state is one fact shared by the "
                "audio and the visual backlog, so this is the same action as ticking it off "
                "on the web."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "news_item_id": {"type": "string"},
                    "read": {"type": "boolean", "default": True},
                },
                "required": ["news_item_id"],
            },
            tool="set_news_item_read",
            transport=transport,
            defaults={"read": True, **_defaults("mark_read")},
        ),
    }


def _highlight_shaper(
    context: SessionContext,
) -> Callable[[Mapping[str, Any]], dict[str, Any] | str]:
    """``{quote, news_item_id?, note?}`` → the arguments Motet's ``save_highlight`` takes.

    Returns a sentence instead of a mapping when nothing matches, which
    :meth:`McpTool.invoke` turns into a failed result rather than a call.
    """

    def shape(arguments: Mapping[str, Any]) -> dict[str, Any] | str:
        quote = str(arguments.get("quote") or "").strip()
        wanted = str(arguments.get("news_item_id") or "").strip() or None
        if wanted is not None and not any(
            segment.news_item_id == wanted for segment in context.transcript
        ):
            # A distinct sentence, because the two failures want different things of the
            # persona: one is "quote me the line", the other is "that story is not in this
            # episode" and no requoting will fix it.
            return (
                f"save_highlight was given a story id ({wanted}) that is not in this "
                f"episode, so there is nothing to save from."
            )
        claim = locate_claim(context, quote, news_item_id=wanted)
        if claim is None:
            return (
                "save_highlight could not find that line in what was read out, so it has "
                "nothing verbatim to save. Quote the narration itself."
            )
        item_id, timed = claim
        shaped: dict[str, Any] = {
            "news_item_id": item_id,
            "source_item_id": timed.source_item_id,
            "span_start": timed.span_start,
            "span_end": timed.span_end,
        }
        if note := str(arguments.get("note") or "").strip():
            shaped["note"] = note
        if context.episode_id:
            # Provenance, never the anchor — AGENTS.md, "highlights anchor to the source
            # span, and nothing else". The span above is the anchor.
            shaped["episode_id"] = context.episode_id
            shaped["anchor_ms"] = timed.start_ms
        return shaped

    return shape


#: How many words a quote needs before it may match by *containment*. A model that
#: under-quotes — ``"that"``, ``"Acme"`` — would otherwise land inside some claim and write
#: a highlight the listener never asked for, which then renders as verbatim source text.
#: That is the failure this whole path exists to prevent, and the paraphrase direction the
#: docstrings talk about is only half of it. A floor on *length* rather than a similarity
#: score: three words is "did the model quote something", not "how close is it".
MIN_CONTAINMENT_WORDS: Final = 3


def locate_claim(
    context: SessionContext, quote: str, *, news_item_id: str | None = None
) -> tuple[str, TimedClaim] | None:
    """The claim in this session's transcript that ``quote`` refers to, with its story's id.

    Exact first, then containment either way, on normalized text — a model repeats a line
    with its own punctuation and capitalization far more often than it repeats it byte for
    byte. Only claims that carry a source span are candidates: a claim without one cannot
    be saved, and offering it would mean sending the API a span nobody has.

    **Within a pass the tightest fit wins, not whichever claim came first**, and that is a
    correctness rule rather than a refinement. When the quote is a fragment *of* a claim the
    shortest containing claim is the most specific; when a claim is a fragment *of* the
    quote the longest one accounts for the most of what was said — and without that, a
    three-word claim ("It was announced.") is contained in almost any paraphrase and would
    win every time, writing the wrong span into a highlight that then reads as verbatim.
    Length rather than a similarity score, deliberately: a threshold here would be a
    judgement about two texts in the one place meant to have no opinion.
    """
    needle = _normalize(quote)
    if not needle:
        return None
    candidates = [
        (item_id, claim, spoken)
        for item_id, claim in _candidates(context, news_item_id)
        if (spoken := _normalize(claim.spoken_text))
    ]
    #: ``(does it match, which of the matches to prefer)``. The key is minimised.
    passes: tuple[tuple[Callable[[str], bool], Callable[[str], int]], ...] = (
        (lambda spoken: spoken == needle, lambda spoken: 0),
        (lambda spoken: needle in spoken, len),
        (lambda spoken: spoken in needle, lambda spoken: -len(spoken)),
    )
    for index, (matches, preference) in enumerate(passes):
        if index and len(needle.split()) < MIN_CONTAINMENT_WORDS:
            # Exact match (pass 0) is always allowed: a one-word claim quoted exactly is the
            # model repeating what was said. Containment on a one-word needle is a guess.
            break
        hits = [
            (item_id, claim, spoken) for item_id, claim, spoken in candidates if matches(spoken)
        ]
        if hits:
            item_id, claim, _ = min(hits, key=lambda hit: preference(hit[2]))
            return item_id, claim
    return None


def _candidates(
    context: SessionContext, news_item_id: str | None
) -> Iterable[tuple[str, TimedClaim]]:
    for segment in context.transcript:
        if segment.news_item_id is None:
            continue
        if news_item_id is not None and segment.news_item_id != news_item_id:
            continue
        for claim in segment.claims:
            if claim.source_item_id and claim.span_end > claim.span_start:
                yield segment.news_item_id, claim


def _normalize(text: str) -> str:
    """Case, punctuation and runs of whitespace folded away. Nothing cleverer: a similarity
    threshold here would be a judgement about two texts in the one place meant to have no
    opinion.

    **Punctuation becomes a space rather than nothing, and the collapse happens after.**
    Deleting it joins the words either side — ``forty-million`` normalizes to
    ``fortymillion`` and matches no paraphrase of it, and an em dash leaves a double space
    that no pass survives. This pipeline's prose is full of both, so the first version of
    this failed to save exactly the lines a listener is most likely to ask for.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()
