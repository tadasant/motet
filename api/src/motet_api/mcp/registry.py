"""Every API operation, classified: a tool, a tool still owed, or a written exclusion.

`api/tests/test_mcp_parity.py` walks `app.routes` and fails on any operation this module
does not classify, and on any entry naming an operation that does not exist. That is the
mechanism motet#111 asks for: zimmer's MCP server keeps parity with a pre-PR skill and a
hand-written table, and the drift is a standing list of issues. Here a route added without
a decision about its MCP counterpart is a red run.

**Adding a route?** Put its ``(METHOD, path)`` in exactly one of:

- a tool's ``covers`` in ``ALL_TOOLS``, once the tool exists;
- ``PLANNED``, if it should be a tool and the tool is not built yet;
- ``EXCLUDED``, with the reason it must never be one.

``PLANNED`` exists because the server is not built yet (motet#111's design picks are
pending). When it is, every entry moves to ``ALL_TOOLS`` and ``PLANNED`` is empty.
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


@dataclass(frozen=True)
class ToolDef:
    name: str
    group: str
    write: bool
    covers: tuple[Operation, ...]


ALL_TOOLS: tuple[ToolDef, ...] = ()
"""Tools that exist. Empty until the server is built."""


PLANNED: tuple[ToolDef, ...] = (
    # backlog
    ToolDef("list_news_items", "backlog", False, (("GET", "/v1/news-items"),)),
    ToolDef(
        "set_news_item_read", "backlog", True, (("POST", "/v1/news-items/{news_item_id}/read"),)
    ),
    # ingestion
    ToolDef("paste_text", "ingestion", True, (("POST", "/v1/sources/paste"),)),
    ToolDef("get_ingestion_status", "ingestion", False, (("GET", "/v1/ingestion"),)),
    ToolDef("list_held_source_items", "ingestion", False, (("GET", "/v1/source-items/held"),)),
    ToolDef("integrate_source_items", "ingestion", True, (("POST", "/v1/source-items/integrate"),)),
    ToolDef("dismiss_source_items", "ingestion", True, (("POST", "/v1/source-items/dismiss"),)),
    ToolDef("get_source_item", "ingestion", False, (("GET", "/v1/source-items/{source_item_id}"),)),
    # sources
    ToolDef("list_sources", "sources", False, (("GET", "/v1/sources"),)),
    ToolDef("connect_source", "sources", True, (("POST", "/v1/sources/connect"),)),
    ToolDef("poll_source", "sources", True, (("POST", "/v1/sources/{source_id}/poll"),)),
    ToolDef("set_label_sync", "sources", True, (("PUT", "/v1/sources/{source_id}/label-sync"),)),
    ToolDef(
        "reauthorize_source", "sources", True, (("POST", "/v1/sources/{source_id}/reauthorize"),)
    ),
    ToolDef(
        "disconnect_source", "sources", True, (("DELETE", "/v1/sources/{source_id}/credentials"),)
    ),
    ToolDef("remove_source", "sources", True, (("DELETE", "/v1/sources/{source_id}"),)),
    # episodes
    ToolDef("list_episodes", "episodes", False, (("GET", "/v1/episodes"),)),
    ToolDef("create_episode", "episodes", True, (("POST", "/v1/episodes"),)),
    ToolDef("create_smart_episode", "episodes", True, (("POST", "/v1/episodes/smart"),)),
    ToolDef("get_episode", "episodes", False, (("GET", "/v1/episodes/{episode_id}"),)),
    ToolDef(
        "set_playback_position", "episodes", True, (("PUT", "/v1/episodes/{episode_id}/position"),)
    ),
    ToolDef(
        "mark_episode_listened", "episodes", True, (("POST", "/v1/episodes/{episode_id}/listened"),)
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
    ToolDef("rotate_feed", "feed", True, (("POST", "/v1/feed/rotate"),)),
    # highlights
    ToolDef("list_highlights", "highlights", False, (("GET", "/v1/highlights"),)),
    ToolDef("save_highlight", "highlights", True, (("POST", "/v1/highlights"),)),
    ToolDef("delete_highlight", "highlights", True, (("DELETE", "/v1/highlights/{highlight_id}"),)),
    # ops
    ToolDef("get_processing_status", "ops", False, (("GET", "/v1/processing"),)),
    ToolDef("get_health", "ops", False, (("GET", "/internal/health"),)),
    ToolDef("get_voice_status", "ops", False, (("GET", "/v1/voice"),)),
    ToolDef("whoami", "ops", False, (("GET", "/v1/auth/session"),)),
    # admin (opt-in)
    ToolDef("logout_everywhere", "admin", True, (("POST", "/v1/auth/logout-all"),)),
    ToolDef("get_admin_overview", "admin", False, (("GET", "/v1/admin/overview"),)),
    ToolDef("list_waitlist", "admin", False, (("GET", "/v1/admin/waitlist"),)),
)
"""Tools owed, named and grouped as proposed on motet#111. Not callable yet."""


EXCLUDED: dict[Operation, str] = {
    ("POST", "/v1/sources/callback"): (
        "The browser redirect leg of a mailbox consent: only /oauth/callback calls it, with a "
        "single-use code Google handed to a browser. connect_source and reauthorize_source "
        "return the consent URL; the click is the human step (invariant 9)."
    ),
    ("POST", "/v1/auth/google/start"): (
        "Signing in is a browser flow an agent cannot complete (Google refuses an automated "
        "browser at the identifier step); an MCP caller already holds a /v1 bearer."
    ),
    ("POST", "/v1/auth/google/callback"): (
        "The redirect leg of signing in; same reason as /v1/auth/google/start."
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
