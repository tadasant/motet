"""Getting content in: pasting, what is on its way, and the held items waiting for a person."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ...schemas import (
    DismissResponse,
    HeldSourceItemResponse,
    IngestionItemResponse,
    IntegrateResponse,
    PasteRequest,
    SourceItemDetailResponse,
    SourceItemIdsRequest,
    SourceItemResponse,
)
from ..context import routes, run

_IDS = Field(description="Source item ids, from list_held_source_items. 1 to 500.")


def paste_text(
    title: Annotated[str, Field(description="A title for the pasted text, e.g. its headline.")],
    text: Annotated[str, Field(description="The article or newsletter text, as plain text.")],
) -> SourceItemResponse:
    """Paste text into Motet as a new source item and queue it for the backlog.

    Pasting is asking: the item is queued for integration straight away, which deduplicates
    it against the backlog with a model call (this spends inference). It reaches the backlog
    as a news item within minutes; `get_ingestion_status` shows it on the way. Returns the
    new source item in state `pending`.
    """
    return run(
        "paste_text",
        lambda c: routes.paste_source(
            body=PasteRequest(title=title.strip(), text=text),
            conn=c.conn,
            user_id=c.user_id,
            nudge=c.nudge,
        ),
    )


def get_ingestion_status() -> list[IngestionItemResponse]:
    """List what has been ingested but is not in the backlog yet, and why.

    Pending items with their attempt count and next retry, failed items with the error, and
    items that landed in the last ten minutes. Use it after `paste_text` or
    `integrate_source_items` to see whether something is stuck. Held items (waiting for a
    person) are `list_held_source_items`, not here.
    """
    return run(
        "get_ingestion_status", lambda c: routes.list_ingestion(conn=c.conn, user_id=c.user_id)
    )


def list_held_source_items() -> list[HeldSourceItemResponse]:
    """List items a connected mailbox pulled in that are waiting to be ingested or dismissed.

    Connecting a source does the free work (poll, fetch, extract) and stops before anything
    spends inference. Each item has a title, a preview and its size. Pass chosen ids to
    `integrate_source_items`, or `dismiss_source_items` to discard them.
    """
    return run(
        "list_held_source_items",
        lambda c: routes.list_held_source_items(conn=c.conn, user_id=c.user_id),
    )


def integrate_source_items(ids: Annotated[list[str], _IDS]) -> IntegrateResponse:
    """Ingest held source items: queue each for deduplication into the backlog.

    This is "Ingest now" on the Backlog screen, and it spends inference, one model call per
    item or more. Ids that are not held (unknown, already queued, dismissed) are skipped,
    not refused. For a Gmail source with label sync set, ingesting also moves the message
    between its labels. Returns how many were queued and skipped.
    """
    return run(
        "integrate_source_items",
        lambda c: routes.integrate_source_items(
            body=SourceItemIdsRequest(ids=ids), conn=c.conn, user_id=c.user_id, nudge=c.nudge
        ),
    )


def dismiss_source_items(ids: Annotated[list[str], _IDS]) -> DismissResponse:
    """Discard held source items without ingesting them. There is no undo.

    A dismissed item stays recorded so a later poll does not pull the message in again.
    Only held items can be dismissed; the rest are skipped. Returns how many were
    dismissed and skipped.
    """
    return run(
        "dismiss_source_items",
        lambda c: routes.dismiss_source_items(
            body=SourceItemIdsRequest(ids=ids), conn=c.conn, user_id=c.user_id
        ),
    )


def get_source_item(
    source_item_id: Annotated[str, Field(description="The source item's id.")],
) -> SourceItemDetailResponse:
    """Get one source item across its lifecycle: its full text, dedup's decision, its news item.

    Shows the extracted text, the deduplication step (whether it merged into an existing
    story, and dedup's stated reason), and the news item it now feeds. Unknown ids are a 404.
    """
    return run(
        "get_source_item",
        lambda c: routes.get_source_item_detail(
            conn=c.conn, user_id=c.user_id, source_item_id=source_item_id
        ),
    )


TOOLS = (
    paste_text,
    get_ingestion_status,
    list_held_source_items,
    integrate_source_items,
    dismiss_source_items,
    get_source_item,
)
