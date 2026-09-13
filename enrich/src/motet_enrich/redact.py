"""Turning a run's event stream into something it is safe to keep.

The raw stream is the worst thing in this system to store. In one spike run it held the
owner's mailbox search results, the body of a sign-in email, a single-use magic link, and
eighteen cookies including a live session. The stored transcript is what makes an
enrichment run explicable afterwards — how many calls, what it cost, whether it logged in,
where it stopped — and none of that needs any of the above.

**Two mechanisms, and the first one is the one that matters.**

1. **A non-browser tool's result is never stored at all.** Not redacted — *replaced*, by a
   note giving its size and its tool. The mailbox search that finds the login email is a
   non-browser tool, so this is the rule that keeps the email's body out of the database,
   and it holds whatever a future connector returns because it is a rule about which
   *server* answered rather than about what the answer looked like.
2. **Everything that is kept goes through :class:`Redactor`.** Known secrets by exact
   match — the site password, each MCP bearer, the login identifier — then patterns for
   the shapes a secret takes when nobody knew it in advance: a bearer header, a
   credential-carrying query parameter, a long opaque path segment (which is what a magic
   link is), a cookie ``"value"``, an email address.

**Rule 1 covers a tool's result and not the model's account of it**, and that gap is real
rather than closed: a model told to read a code out of an email can restate it in its own
message, where only rule 2 stands between it and the database. What narrows it is that the
*only* assistant text stored is the final answer, with the article's fenced block taken out
(:func:`strip_article`) — so the transcript holds the agent's two-line verdict and its
reasoning about being blocked, not a running narration of the mailbox. The prompt tells it
not to repeat anything from the mailbox; that is a mitigation, and this paragraph exists so
that nobody reads it as the control.

**The pattern half is a backstop and cannot be complete**, which is the honest statement
of what this module buys. A site that puts a session token in a shape none of these
matches would have it stored. That is why rule 1 is first and is a rule about provenance:
it needs to guess nothing.

Truncation is not redaction and is not treated as such — it is here because a stored
transcript is read in a list, and a 20,000-character article body in a tool result makes
the row useless as well as large.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

#: The server slug whose tool results may be stored. Only this one's.
#:
#: Matched on the *namespace* of the tool name the MCP adapter registers, which is
#: ``<server>__<tool>`` — see :func:`is_browser_tool`. The slug is reserved: a connector
#: may not be handed to a run under it (``motet_enrich.pi._mcp_document`` refuses), and the
#: worker derives a connector's slug from its id rather than from the owner's label, so a
#: connector labelled "browser" cannot reach it either.
BROWSER_SERVER: Final = "browser"

#: How much of a kept string is kept. Generous enough that a tool call's arguments are
#: still readable, small enough that a hundred of them are a row rather than a document.
MAX_KEPT_CHARS: Final = 2_000

REDACTED: Final = "<redacted>"

#: Query parameters whose *value* is a credential often enough that keeping one is not
#: worth the exception. ``eu`` is TheInformation's per-recipient article token, which the
#: spike found reads the whole article without a login — so it is a bearer secret in a URL
#: in exactly the way the RSS feed token is.
_SECRET_PARAMS: Final = frozenset(
    {
        "access_token",
        "auth",
        "code",
        "credential",
        "eu",
        "id_token",
        "key",
        "password",
        "refresh_token",
        "secret",
        "session",
        "sig",
        "signature",
        "state",
        "token",
        "upn",
    }
)

_QUERY_RE: Final = re.compile(r"(?i)\b(" + "|".join(sorted(_SECRET_PARAMS)) + r")=([^&\s\"'<>\\]+)")

#: ``Authorization: Bearer xyz``, and the bare ``Bearer xyz`` an argument might carry.
_BEARER_RE: Final = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

#: A cookie document's value field, as `storageState()` writes it. The harness keeps the
#: cookies out of the model's hands entirely, so this only fires if a page put one in its
#: own text — but that is exactly the case nobody would predict.
_COOKIE_VALUE_RE: Final = re.compile(r'("value"\s*:\s*")([^"]{8,})(")')

_EMAIL_RE: Final = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

#: A URL path segment long enough and random enough to be a token rather than a slug.
#:
#: A magic link is exactly this: ``/sessions/confirm/8f3a...`` with forty characters of
#: base64 on the end. An article slug is words and hyphens, so requiring **no hyphen and no
#: dot** keeps ``/articles/openai-raises-again`` intact while catching the opaque one. The
#: threshold is 24 because a shorter opaque segment is more often an id than a secret, and
#: an id is worth reading.
#:
#: The lookahead is where the segment must *end*: a path separator, a query, or whitespace
#: and the punctuation a URL inside prose is followed by. A hyphen and a dot are
#: deliberately absent from it, which is what stops a long hyphenated slug from having its
#: first word eaten.
_OPAQUE_SEGMENT_RE: Final = re.compile(r"""(?<=/)[A-Za-z0-9_~%+=]{24,}(?=[/?#\s"'<>)\],;]|$)""")


def _exact_needles(values: Iterable[str | None]) -> list[str]:
    """The secrets we were handed, longest first so a prefix cannot mask a longer match."""
    seen = {value.strip() for value in values if value and len(value.strip()) >= 4}
    return sorted(seen, key=len, reverse=True)


class Redactor:
    """Removes this run's known secrets, then the shapes a secret usually takes."""

    def __init__(self, secrets: Sequence[str | None] = ()) -> None:
        self._needles = _exact_needles(secrets)

    def __call__(self, text: str | None) -> str | None:
        if text is None:
            return None
        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, REDACTED)
        text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
        text = _COOKIE_VALUE_RE.sub(rf"\1{REDACTED}\3", text)
        text = _QUERY_RE.sub(rf"\1={REDACTED}", text)
        text = _OPAQUE_SEGMENT_RE.sub(REDACTED, text)
        text = _EMAIL_RE.sub(REDACTED, text)
        return text

    def clip(self, text: str | None) -> str | None:
        """Redact, then bound the length. Both, in that order, or neither is true."""
        cleaned = self(text)
        if cleaned is None or len(cleaned) <= MAX_KEPT_CHARS:
            return cleaned
        return cleaned[:MAX_KEPT_CHARS] + f"… (+{len(cleaned) - MAX_KEPT_CHARS} chars)"


def is_browser_tool(tool: str | None) -> bool:
    """Whether a tool result may be stored at all: **the namespace, and nothing else.**

    A tool name is ``<server>__<tool>`` and the server half is the only part of it this
    process controls — the tool half is whatever a remote server chose to call itself.
    An earlier version also accepted a bare ``browser_*`` prefix, for the case where the
    adapter does not namespace; that handed result retention to any connected MCP server
    that exposed a tool called ``browser_search`` — and the owner's mailbox server is
    exactly the one where that matters. There is always a namespace here, because
    :mod:`motet_enrich.pi` always registers the browser server under
    :data:`BROWSER_SERVER`, so nothing is lost by requiring one.
    """
    if not tool:
        return False
    server, separator, rest = tool.partition("__")
    return bool(separator) and bool(rest) and server == BROWSER_SERVER


#: The fenced block the agent returns the article in. Kept out of the transcript: it is
#: already `source_items.text`, storing it twice doubles the largest row in the table, and
#: an article is the one part of the answer with no forensic value.
_ARTICLE_FENCE_RE: Final = re.compile(r"```ARTICLE_MARKDOWN.*?```", re.DOTALL)


def strip_article(text: str | None) -> str | None:
    """The agent's final message without the article it carried."""
    if text is None:
        return None
    return _ARTICLE_FENCE_RE.sub("<article, stored on the item>", text)


def summarize_foreign_result(tool: str, result: str | None) -> str:
    """What a non-browser tool's result is stored as: its size, and nothing else."""
    return f"<{len(result or '')} chars from {tool}, not stored>"
