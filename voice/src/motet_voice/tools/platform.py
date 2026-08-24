"""The four platform tools, and the HTTP transport that reaches Motet's API.

```
save_highlight   get_item_detail   start_research   mark_read
```

**Every one of them is an HTTP call to Motet's own API**, never a query. That is invariant
3, and it is also what makes these tools work for Zimmer later: a caller with a different
backend points the transport somewhere else and the tools are unchanged.

**Three of the four depend on routes that are not all merged yet**, and this file codes to
the contract rather than reaching into another session's tree:

| Tool | Route | Status |
|---|---|---|
| ``mark_read`` | ``POST /v1/news-items/{id}/read`` | **Shipped** — Phase 1 |
| ``get_item_detail`` | ``GET /v1/news-items/{id}`` | Needs the single-item route |
| ``save_highlight`` | ``POST /v1/highlights`` | Needs the highlights surface |
| ``start_research`` | — | Needs **Exa**, which is not provisioned |

The first three become live the moment their routes exist: nothing here changes, because a
missing route is a 404 the tool reports rather than a shape it has to know about in advance.
``start_research`` is dormant on a credential, which is a different kind of missing and is
reported as such.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx

from ..config import EXA_KEY_ENV, VoiceSettings
from .spec import (
    AVAILABLE,
    ToolAvailability,
    ToolResponse,
    ToolResult,
    ToolState,
    ToolTransport,
)

#: Short on purpose. A tool call happens inside a conversational turn — a listener is
#: standing on a pavement waiting for an answer. Ten seconds of silence is a failed turn
#: whatever the eventual HTTP status says.
DEFAULT_TOOL_TIMEOUT_SECONDS: Final = 8.0


class HttpToolTransport:
    """Calls Motet's API over HTTP with a scoped bearer token.

    The base URL and the token come from this service's own environment. Neither is ever
    accepted from a client: a client-supplied URL turns this into an open proxy, and a
    client-supplied token turns it into a confused deputy.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    async def request(
        self, method: str, path: str, *, json: Mapping[str, Any] | None = None
    ) -> ToolResponse:
        try:
            response = await self._client.request(method, path, json=dict(json or {}))
        except httpx.HTTPError as exc:
            # Mapped to a status rather than raised: the registry turns a failed result
            # into something the persona can say, and a transport blow-up mid-turn is the
            # one thing a voice session cannot recover from gracefully.
            return ToolResponse(status=599, payload={"detail": f"could not reach the API: {exc}"})
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text[:500]}
        return ToolResponse(
            status=response.status_code,
            payload=payload if isinstance(payload, dict) else {"data": payload},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass
class ApiTool:
    """A platform tool backed by one API route.

    One class rather than four, because the four differ only in method, path, and how they
    shape a payload — and a base class per tool is four places for the defaults-merging
    rule to be got subtly wrong.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    method: str
    transport: ToolTransport | None = None
    #: Bound by the caller at StartSession — the episode a session is about, say. Merged
    #: **under** the model's arguments, never over them.
    defaults: dict[str, Any] = field(default_factory=dict)
    unavailable_reason: str = ""
    #: Path arguments are interpolated, not concatenated, and are URL-quoted here.
    path_template: str = ""
    #: Which argument names go in the path rather than the body.
    path_arguments: tuple[str, ...] = ()

    def availability(self) -> ToolAvailability:
        if self.unavailable_reason:
            return ToolAvailability(ToolState.DORMANT, self.unavailable_reason)
        if self.transport is None:
            return ToolAvailability(
                ToolState.DORMANT,
                "the voice service has no Motet API base URL configured, so this tool "
                "cannot reach anything",
            )
        return AVAILABLE

    async def invoke(self, arguments: Mapping[str, Any]) -> ToolResult:
        if self.transport is None:  # pragma: no cover — the registry checks availability
            return ToolResult.failure("no transport configured")

        merged: dict[str, Any] = {**self.defaults, **dict(arguments)}
        try:
            path = self.path_template.format(
                **{key: quote(str(merged[key]), safe="") for key in self.path_arguments}
            )
        except KeyError as exc:
            return ToolResult.failure(f"{self.name} needs a {exc.args[0]}")

        body = {key: value for key, value in merged.items() if key not in self.path_arguments}
        response = await self.transport.request(
            self.method, path, json=None if self.method == "GET" else body
        )
        if not response.ok:
            return ToolResult.failure(_explain(self.name, response))
        return ToolResult(ok=True, result=response.payload)


def _explain(name: str, response: ToolResponse) -> str:
    detail = str(response.payload.get("detail") or response.payload.get("error") or "").strip()
    if response.status == 404:
        return (
            f"{name} got a 404 from the Motet API. Either the item does not exist, or the "
            f"route it needs has not shipped yet."
        )
    return f"{name} failed with status {response.status}" + (f": {detail}" if detail else "")


def build_platform_tools(
    settings: VoiceSettings,
    *,
    transport: ToolTransport | None = None,
    defaults: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ApiTool]:
    """The four platform tools, wired to a transport and told what they cannot do."""
    bound = defaults or {}

    def _defaults(name: str) -> dict[str, Any]:
        return dict(bound.get(name, {}))

    return {
        "save_highlight": ApiTool(
            name="save_highlight",
            description=(
                "Save something the listener wants to keep from the story being discussed. "
                "Use it when they say 'save that', 'remember this', or quote a line back."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "news_item_id": {"type": "string", "description": "The story it belongs to."},
                    "quote": {"type": "string", "description": "The words worth keeping."},
                    "note": {"type": "string", "description": "Why, in the listener's words."},
                },
                "required": ["news_item_id", "quote"],
            },
            method="POST",
            path_template="/v1/highlights",
            transport=transport,
            defaults=_defaults("save_highlight"),
        ),
        "get_item_detail": ApiTool(
            name="get_item_detail",
            description=(
                "Fetch the full detail of one news item — its summary, its sources, and the "
                "spans behind its claims. Use it when asked to go deeper on a story."
            ),
            parameters={
                "type": "object",
                "properties": {"news_item_id": {"type": "string"}},
                "required": ["news_item_id"],
            },
            method="GET",
            path_template="/v1/news-items/{news_item_id}",
            path_arguments=("news_item_id",),
            transport=transport,
            defaults=_defaults("get_item_detail"),
        ),
        "start_research": ApiTool(
            name="start_research",
            description=(
                "Kick off background research on a question the briefing does not answer, "
                "to be folded into a later episode."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "news_item_id": {"type": "string"},
                },
                "required": ["query"],
            },
            method="POST",
            path_template="/v1/research",
            transport=transport,
            defaults=_defaults("start_research"),
            unavailable_reason=(
                "background research needs the Exa search vendor, which is not provisioned "
                f"({EXA_KEY_ENV} is unset). Offer to note the question instead."
            )
            if not settings.exa_api_key_present
            else "",
        ),
        "mark_read": ApiTool(
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
            method="POST",
            path_template="/v1/news-items/{news_item_id}/read",
            path_arguments=("news_item_id",),
            transport=transport,
            defaults={"read": True, **_defaults("mark_read")},
        ),
    }
