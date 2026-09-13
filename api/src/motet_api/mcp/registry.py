"""Every API operation, classified: a tool, or a written exclusion (motet#111).

`api/tests/test_mcp_parity.py` walks `app.routes` and fails on any operation this module
does not classify, and on any entry naming an operation that does not exist. That is the
mechanism motet#111 asks for. Zimmer's MCP server keeps parity with a pre-PR skill and a
hand-written table, and its drift is a standing list of issues; here a route added without
a decision about its MCP counterpart is a red run.

**Adding a route?** Put its ``(METHOD, path)`` in exactly one of:

- a tool's ``covers`` in ``ALL_TOOLS``, with the tool in ``motet_api.mcp.tools``;
- ``EXCLUDED``, with the reason it must never be a tool.

**Groups** are what a connection asks for with ``?tool_groups=``, read from the query string
and nothing else, so a request body cannot widen what a connection may call. With none named
a connection gets ``DEFAULT_GROUPS``. ``admin`` is in ``OPT_IN_GROUPS`` and is never on the
default surface. Every group also has a ``<group>_readonly`` variant that drops its write
tools, which is what a caller that should only look (the voice service, later) is given.
"""

from __future__ import annotations

from dataclasses import dataclass

Operation = tuple[str, str]
"""``(METHOD, path)``, with the path exactly as FastAPI declares it."""

DEFAULT_GROUPS: tuple[str, ...] = (
    "backlog",
    "ingestion",
    "sources",
    "episodes",
    "feed",
    "highlights",
    "ops",
)
"""The groups a connection gets when it names none."""

OPT_IN_GROUPS: tuple[str, ...] = ("admin",)
"""Groups a connection gets only by naming them. Never on the default surface."""

READONLY_SUFFIX = "_readonly"

GROUPS_PARAM = "tool_groups"


@dataclass(frozen=True)
class ToolDef:
    name: str
    group: str
    #: Whether the tool changes anything. Drives ``read_only_hint`` and the ``_readonly``
    #: variants, and the parity test holds it equal to "covers a non-GET operation".
    write: bool
    covers: tuple[Operation, ...]
    #: Whether the change is one a person would want to be asked about: it deletes, or it
    #: breaks something that already works elsewhere (a feed URL in a podcast app).
    destructive: bool = False
    #: Whether calling it twice with the same arguments is the same as calling it once.
    idempotent: bool = False


ALL_TOOLS: tuple[ToolDef, ...] = (
    # backlog
    ToolDef("list_news_items", "backlog", False, (("GET", "/v1/news-items"),)),
    ToolDef(
        "set_news_item_read",
        "backlog",
        True,
        (("POST", "/v1/news-items/{news_item_id}/read"),),
        idempotent=True,
    ),
    # ingestion
    ToolDef("paste_text", "ingestion", True, (("POST", "/v1/sources/paste"),)),
    ToolDef("get_ingestion_status", "ingestion", False, (("GET", "/v1/ingestion"),)),
    ToolDef("list_held_source_items", "ingestion", False, (("GET", "/v1/source-items/held"),)),
    ToolDef(
        "integrate_source_items",
        "ingestion",
        True,
        (("POST", "/v1/source-items/integrate"),),
        idempotent=True,
    ),
    ToolDef(
        "dismiss_source_items",
        "ingestion",
        True,
        (("POST", "/v1/source-items/dismiss"),),
        destructive=True,
        idempotent=True,
    ),
    ToolDef("get_source_item", "ingestion", False, (("GET", "/v1/source-items/{source_item_id}"),)),
    # sources
    ToolDef("list_sources", "sources", False, (("GET", "/v1/sources"),)),
    ToolDef("connect_source", "sources", True, (("POST", "/v1/sources/connect"),)),
    ToolDef("poll_source", "sources", True, (("POST", "/v1/sources/{source_id}/poll"),)),
    ToolDef(
        "set_label_sync",
        "sources",
        True,
        (("PUT", "/v1/sources/{source_id}/label-sync"),),
        idempotent=True,
    ),
    ToolDef(
        "reauthorize_source", "sources", True, (("POST", "/v1/sources/{source_id}/reauthorize"),)
    ),
    ToolDef(
        "disconnect_source",
        "sources",
        True,
        (("DELETE", "/v1/sources/{source_id}/credentials"),),
        destructive=True,
        idempotent=True,
    ),
    ToolDef(
        "remove_source",
        "sources",
        True,
        (("DELETE", "/v1/sources/{source_id}"),),
        destructive=True,
    ),
    # episodes
    ToolDef("list_episodes", "episodes", False, (("GET", "/v1/episodes"),)),
    ToolDef("create_episode", "episodes", True, (("POST", "/v1/episodes"),)),
    ToolDef("create_smart_episode", "episodes", True, (("POST", "/v1/episodes/smart"),)),
    ToolDef("get_episode", "episodes", False, (("GET", "/v1/episodes/{episode_id}"),)),
    ToolDef(
        "set_playback_position",
        "episodes",
        True,
        (("PUT", "/v1/episodes/{episode_id}/position"),),
        idempotent=True,
    ),
    ToolDef(
        "mark_episode_listened",
        "episodes",
        True,
        (("POST", "/v1/episodes/{episode_id}/listened"),),
        idempotent=True,
    ),
    ToolDef(
        "get_episode_transcript",
        "episodes",
        False,
        (("GET", "/v1/episodes/{episode_id}/transcript.vtt"),),
    ),
    ToolDef(
        "get_episode_chapters",
        "episodes",
        False,
        (("GET", "/v1/episodes/{episode_id}/chapters.json"),),
    ),
    # feed
    ToolDef("get_feed", "feed", False, (("GET", "/v1/feed"),)),
    ToolDef("rotate_feed", "feed", True, (("POST", "/v1/feed/rotate"),), destructive=True),
    # highlights
    ToolDef("list_highlights", "highlights", False, (("GET", "/v1/highlights"),)),
    ToolDef("save_highlight", "highlights", True, (("POST", "/v1/highlights"),)),
    ToolDef(
        "delete_highlight",
        "highlights",
        True,
        (("DELETE", "/v1/highlights/{highlight_id}"),),
        destructive=True,
    ),
    # ops
    ToolDef("get_processing_status", "ops", False, (("GET", "/v1/processing"),)),
    ToolDef("get_health", "ops", False, (("GET", "/internal/health"),)),
    ToolDef("get_voice_status", "ops", False, (("GET", "/v1/voice"),)),
    ToolDef("whoami", "ops", False, (("GET", "/v1/auth/session"),)),
    # admin (opt-in)
    ToolDef(
        "logout_everywhere",
        "admin",
        True,
        (("POST", "/v1/auth/logout-all"),),
        destructive=True,
        idempotent=True,
    ),
    ToolDef("get_admin_overview", "admin", False, (("GET", "/v1/admin/overview"),)),
    ToolDef("list_waitlist", "admin", False, (("GET", "/v1/admin/waitlist"),)),
    ToolDef("get_llm_config", "admin", False, (("GET", "/v1/admin/llm-config"),)),
    ToolDef(
        "set_llm_config",
        "admin",
        True,
        (("PUT", "/v1/admin/llm-config/{stage}"),),
        idempotent=True,
    ),
    ToolDef("get_llm_spend", "admin", False, (("GET", "/v1/admin/llm-spend"),)),
)


EXCLUDED: dict[Operation, str] = {
    ("POST", "/v1/sources/callback"): (
        "The browser redirect leg of a mailbox consent: only /oauth/callback calls it, with a "
        "single-use code Google handed to a browser. connect_source and reauthorize_source "
        "return the consent URL; the click is the human step (invariant 9)."
    ),
    ("POST", "/v1/auth/google/start"): (
        "Signing in is a browser flow an agent cannot complete (Google refuses an automated "
        "browser at the identifier step); an MCP caller already holds a bearer."
    ),
    ("POST", "/v1/auth/google/callback"): (
        "The redirect leg of signing in; same reason as /v1/auth/google/start."
    ),
    ("POST", "/v1/auth/mcp/callback"): (
        "The redirect leg of an MCP client's own authorization: the SPA posts Google's code "
        "here on the way back to the client. It is how an MCP connection gets a token, so it "
        "cannot be something that connection calls."
    ),
    ("POST", "/v1/auth/logout"): (
        "Revokes the credential the call arrived on, which would end the MCP connection that "
        "made it. logout_everywhere (admin) is the revocation an agent may need."
    ),
    ("POST", "/v1/episodes/{episode_id}/progress"): (
        "The same handler as PUT /v1/episodes/{episode_id}/position, two decorators on one "
        "function kept for shipped clients. One fact gets one tool (invariant 5): "
        "set_playback_position."
    ),
    ("GET", "/v1/episodes/{episode_id}/audio"): (
        "Serves audio bytes or a redirect to a signed URL. Audio is not a tool result, and the "
        "RSS feed is the listening surface."
    ),
    ("GET", "/feed.xml"): (
        "Authenticated by the feed token, which is a bearer secret for one read-only document "
        "and is never an MCP credential. get_feed returns the subscription URL."
    ),
    ("GET", "/v1/feed/artwork.png"): (
        "The podcast artwork image the feed's <itunes:image> points at, served without a "
        "credential to podcast apps' image caches. Image bytes are not a tool result."
    ),
    ("POST", "/v1/waitlist"): (
        "The landing page's public, credential-free signup form for people who are not users. "
        "An MCP caller is already an authenticated user and has nothing to join."
    ),
    ("POST", "/v1/episodes/{episode_id}/voice-session"): (
        "Mints a voice socket URL and an authenticate frame that only a realtime audio client "
        "can use; a tool result cannot carry a microphone. get_voice_status reports whether "
        "Play Live is available."
    ),
}


class ToolGroupsError(ValueError):
    """A ``tool_groups`` value names a group that does not exist."""


def all_group_names() -> tuple[str, ...]:
    """Every value ``tool_groups`` accepts, in the order a reader would want them listed."""
    groups = (*DEFAULT_GROUPS, *OPT_IN_GROUPS)
    return (*groups, *(f"{group}{READONLY_SUFFIX}" for group in groups))


def select_tools(raw: str | None) -> frozenset[str]:
    """The tool names a connection may list and call, from its ``tool_groups`` value.

    Comma-separated, whitespace ignored. Naming a group and its ``_readonly`` variant gives
    the whole group. An unknown name is refused rather than ignored: a typo that silently
    produced a smaller surface would read as a missing tool rather than a wrong URL.
    """
    known = set(DEFAULT_GROUPS) | set(OPT_IN_GROUPS)
    tokens = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not tokens:
        full = set(DEFAULT_GROUPS)
        readonly: set[str] = set()
    else:
        full, readonly = set(), set()
        for token in tokens:
            base, is_readonly = (
                (token.removesuffix(READONLY_SUFFIX), True)
                if token.endswith(READONLY_SUFFIX)
                else (token, False)
            )
            if base not in known:
                raise ToolGroupsError(
                    f"Unknown tool group {token!r}. Groups are: {', '.join(all_group_names())}."
                )
            (readonly if is_readonly else full).add(base)
    return frozenset(
        tool.name
        for tool in ALL_TOOLS
        if tool.group in full or (tool.group in readonly and not tool.write)
    )
