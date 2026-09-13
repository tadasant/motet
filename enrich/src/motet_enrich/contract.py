"""The request and the answer `motet-worker` and `motet-enrich` agree on.

**This module is the seam, and it is imported by both sides.** ``motet-workers`` depends on
``motet-enrich`` for exactly this file; a second hand-written copy of these shapes on the
worker side would be two definitions of one contract, and the first field either of them
grew would make them disagree silently over HTTP.

The shape follows from design option D2 (motet#102), which is a statement about *who holds
what*:

* The **worker** holds the KMS decrypt path (invariant 8). It opens the one site login, the
  MCP token sets and the sealed browser state that one run needs, and sends them in the
  request body — in transit only, never at rest in this service.
* The **service** holds none of that. It has no database, no key, and no identity worth
  minting: it runs the agent and answers with the article, a **redacted** transcript and
  the browser's new cookies, which the worker seals and stores.

So every secret in :class:`EnrichRequest` is short-lived and scoped to one run, and
everything in :class:`EnrichResult` has already been through :mod:`motet_enrich.redact`
before it is serialized — see ``motet_enrich.pi._redacted``, which applies both of that
module's rules.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

#: How a run ended.
#:
#: ``ok`` is the only one that produced an article. The other four are all "the preview is
#: what the briefing gets", and they are kept apart because they want different answers:
#: ``blocked`` is the agent saying it could not get past something (a wall it has no
#: credential for), ``capped`` is one of the caps in :class:`RunCaps` firing, ``timeout``
#: is the wall clock, and ``failed`` is everything else — a crashed toolchain, an
#: unreadable answer, a vendor refusal.
RunStatus = Literal["ok", "blocked", "capped", "timeout", "failed"]


class SiteCredential(BaseModel):
    """The owner's login for the one site this run is allowed to touch.

    Both halves are optional and that is the design, not laxity: adding a ``site`` row is
    the opt-in (option B3), and a site readable from the newsletter's own link needs no
    credential at all while a site that emails a magic link needs only the address.
    """

    domain: str = Field(
        min_length=1,
        description=(
            "The registrable domain, normalized: 'example.com'. Non-empty, because it seeds "
            "the browser's navigation allowlist and an empty one would widen it."
        ),
    )
    username: str | None = Field(
        default=None, description="The login identifier, when the site needs one."
    )
    password: str | None = Field(
        default=None, description="The password, when the site has one. Often absent."
    )


class McpServer(BaseModel):
    """A remote MCP server the owner authorized, with a bearer good for this run.

    The token is already refreshed by the worker — this service cannot refresh one, because
    refreshing means re-sealing and only the worker may do that.

    **This is the prompt-injection surface the design names out loud** (option E1): the
    agent that is handed this server also reads pages nobody at Motet wrote. The owner
    acknowledged that when the connector was created; what this service adds is that no
    tool result from a non-browser server is ever stored (see
    :mod:`motet_enrich.redact`).
    """

    name: str = Field(
        description=(
            "A short slug, unique within the run. Becomes the tool namespace, and 'browser' "
            "is reserved for the harness — see motet_enrich.pi._mcp_document, which refuses "
            "it rather than letting a server replace the locked browser."
        ),
        pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$",
    )
    url: str = Field(description="The server URL, query string and all.")
    access_token: str = Field(description="A bearer valid for the length of this run.")


class RunCaps(BaseModel):
    """What bounds one run (design option C2).

    A cap is a **recorded skip**, never a retry: hitting one means the agent had its
    budget and did not finish, and trying again would spend the same money to learn the
    same thing. The per-user-per-day dollar cap is deliberately *not* here — it is a
    question about rows this service cannot see, so the worker answers it before it calls.
    """

    max_usd: Annotated[float, Field(gt=0)] = Field(
        description="Abort once the agent's cumulative cost passes this."
    )
    max_tool_calls: Annotated[int, Field(gt=0)] = Field(
        description=(
            "How many tool calls the agent may make. It is stopped on the one after, so "
            "this many are allowed to complete rather than this many attempted."
        )
    )
    timeout_seconds: Annotated[int, Field(gt=0)] = Field(
        description="Abort the whole run, and everything it started, after this long."
    )


class EnrichRequest(BaseModel):
    """One item to enrich, with exactly the credentials that item needs.

    ``candidate_urls`` are the links **the newsletter itself carried** whose host belongs to
    ``site.domain`` — not a search, and not anything the agent chose. They are where the
    navigation lock starts: the browser is confined to ``site.domain`` and to the hosts
    named here, so a hostile page cannot steer the agent onto a third domain.
    """

    item_id: str = Field(description="The source item, for the log line. Never shown to the model.")
    title: str = Field(description="The newsletter's subject line.")
    preview_text: str = Field(
        default="", description="The extracted newsletter body, as the briefing would use it."
    )
    candidate_urls: list[str] = Field(
        min_length=1,
        description="Links from the newsletter on this site's domain, in document order.",
    )
    site: SiteCredential
    mcp_servers: list[McpServer] = Field(default_factory=list)
    browser_state: str | None = Field(
        default=None,
        description=(
            "A Playwright storage-state document from this user's last run on this domain, "
            "opened by the worker. Seeded into the browser context before the agent starts, "
            "and never shown to the model — the spike paid 13.7k output tokens for the "
            "version where the model handled it."
        ),
    )
    caps: RunCaps


class TranscriptEntry(BaseModel):
    """One line of the **redacted** transcript, as it will be stored.

    ``result`` is present only for the browser server's own calls. Anything a *non-browser*
    tool returned is replaced by a note giving its size, because the login email's body is
    precisely the thing that must not be written to a database (see
    :mod:`motet_enrich.redact`).
    """

    seq: int
    kind: Literal["tool_call", "tool_result", "text", "error"]
    tool: str | None = None
    args: str | None = None
    ok: bool | None = None
    result: str | None = None
    text: str | None = None
    cost_usd: float | None = None


class EnrichResult(BaseModel):
    """What one run produced.

    ``browser_state`` comes back on **every** outcome, including a timeout: the harness
    writes the storage state after each browser call, so a run that logged in and then ran
    out of clock still hands back the cookies that login bought. The worker seals whatever
    is here.
    """

    status: RunStatus
    article_url: str | None = None
    article_markdown: str | None = None
    login_performed: bool = False
    tool_calls: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    browser_state: str | None = None
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    error: str | None = Field(
        default=None, description="Why it is not 'ok'. Redacted like the transcript."
    )


class EnrichHealth(BaseModel):
    """What this service can say about itself without a database or a vendor call."""

    status: Literal["ok"]
    service: str
    revision: str | None
    telemetry_configured: bool
    telemetry_exporting: bool
    errors_configured: bool
    authenticated: bool = Field(
        description=(
            "Whether POST /v1/enrich requires the shared bearer. False means anyone who "
            "can reach this process can spend its OpenRouter key — legitimate on a laptop, "
            "a mistake anywhere else."
        )
    )
    inference_mode: str
    toolchain_ready: bool = Field(
        description=(
            "Whether the real runner could start: the pi CLI, the MCP adapter, the stealth "
            "browser server and a Chromium are all present. False in an image that carries "
            "only the Python half, and false is what an unconfigured container reports "
            "rather than a 500 an hour later."
        )
    )
    toolchain_detail: str | None = Field(
        default=None, description="Which piece is missing, when toolchain_ready is false."
    )
    model: str = Field(description="The slug the agent runs on.")
    max_usd_per_item: float
    max_tool_calls: int
    timeout_seconds: int
