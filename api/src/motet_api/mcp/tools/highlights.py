"""Highlights: passages saved from a story's source text."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...schemas import HighlightResponse, SaveHighlightRequest
from ..context import ActionResult, RouteCall, routes, run


def list_highlights() -> list[HighlightResponse]:
    """List every saved highlight, newest first, with its quote and note."""
    return run("list_highlights", lambda c: routes.list_highlights(conn=c.conn, user_id=c.user_id))


def save_highlight(
    news_item_id: Annotated[str, Field(description="The story the passage belongs to.")],
    source_item_id: Annotated[str, Field(description="The source item the passage is in.")],
    span_start: Annotated[int, Field(description="Start character offset in the source text.")],
    span_end: Annotated[int, Field(description="End character offset (exclusive).")],
    note: Annotated[str | None, Field(description="An optional note to keep with it.")] = None,
    episode_id: Annotated[
        str | None, Field(description="The episode it was heard in, if any.")
    ] = None,
    anchor_ms: Annotated[
        int | None, Field(description="Where in that episode, in milliseconds.")
    ] = None,
) -> HighlightResponse:
    """Save a passage from a source item as a highlight.

    The quote is read out of the source text at the span given, never taken from the
    caller, so a highlight is always verbatim. Get spans from a claim's `span` in
    `get_episode`, or from `get_source_item`'s text.
    """
    return run(
        "save_highlight",
        lambda c: routes.save_highlight(
            body=SaveHighlightRequest(
                news_item_id=news_item_id,
                source_item_id=source_item_id,
                span_start=span_start,
                span_end=span_end,
                note=note,
                episode_id=episode_id,
                anchor_ms=anchor_ms,
            ),
            conn=c.conn,
            user_id=c.user_id,
        ),
    )


def delete_highlight(
    highlight_id: Annotated[str, Field(description="The highlight's id, from list_highlights.")],
) -> ActionResult:
    """Delete a saved highlight. There is no undo."""

    def call(c: RouteCall) -> ActionResult:
        routes.delete_highlight(conn=c.conn, user_id=c.user_id, highlight_id=highlight_id)
        return ActionResult(done=True, detail=f"Deleted {highlight_id}.")

    return run("delete_highlight", call)


TOOLS = (list_highlights, save_highlight, delete_highlight)
