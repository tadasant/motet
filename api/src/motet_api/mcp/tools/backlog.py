"""The backlog: deduplicated news items and whether each has been read."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...schemas import NewsItemResponse, ReadStateRequest
from ..context import routes, run


def list_news_items() -> list[NewsItemResponse]:
    """List the backlog: every deduplicated news item, oldest first, with its read state.

    A news item is one story, merged from however many newsletters or pastes covered it;
    `source_item_ids` and `source_titles` say which. `read_at` is null for an unread item.
    Use this to decide what an episode would contain, or to find a `news_item_id` for
    `set_news_item_read` or `save_highlight`.
    """
    return run("list_news_items", lambda c: routes.list_news_items(conn=c.conn, user_id=c.user_id))


def set_news_item_read(
    news_item_id: Annotated[str, Field(description="The news item's id, from list_news_items.")],
    read: Annotated[bool, Field(description="True marks it read; false marks it unread.")] = True,
) -> NewsItemResponse:
    """Mark one news item read or unread.

    This is the same fact as having listened past the story in an episode (invariant 5):
    a read item is left out of the next "everything unread" episode. Returns the updated
    item. Unknown ids are a 404.
    """
    return run(
        "set_news_item_read",
        lambda c: routes.set_news_item_read(
            body=ReadStateRequest(read=read),
            conn=c.conn,
            user_id=c.user_id,
            news_item_id=news_item_id,
        ),
    )


TOOLS = (list_news_items, set_news_item_read)
