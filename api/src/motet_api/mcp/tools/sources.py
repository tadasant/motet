"""Connected sources: mailboxes, their polling, label sync, and consent."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ...config import CALLBACK_PATH, Settings
from ...schemas import (
    ConnectSourceRequest,
    ConnectSourceResponse,
    LabelSyncRequest,
    ReauthorizeSourceRequest,
    ResyncRequest,
    SourceResponse,
)
from ..context import ActionResult, RouteCall, routes, run

_SOURCE_ID = Field(description="The source's id, from list_sources.")
_FIRST_SYNC_DAYS = Field(
    description=(
        "How far back the first sync reaches, in days. Omit to use the deployment's "
        "default. The ceiling is 3650 (ten years), which is effectively 'everything'."
    )
)
_REDIRECT = Field(
    description=(
        "Where Google returns the person after consent. Omit it to use this deployment's "
        "web app, which is almost always right."
    )
)


def consent_redirect(config: Settings, given: str | None) -> str:
    if given:
        return given
    if config.app_base_url:
        return f"{config.app_base_url.rstrip('/')}{CALLBACK_PATH}"
    raise ToolError(
        f"This deployment has no web app origin configured, so pass redirect_uri (the web "
        f"app's origin plus {CALLBACK_PATH})."
    )


def list_sources() -> list[SourceResponse]:
    """List every source (the paste source and each connected mailbox) and its state.

    Each source says whether it is active and has a credential, its Gmail filter, what its
    last sync found, how many items it has pulled in, and its label-sync setting. Use it to
    find a `source_id`.
    """
    return run("list_sources", lambda c: routes.list_sources(conn=c.conn, user_id=c.user_id))


def connect_source(
    name: Annotated[str, Field(description="A name for the mailbox, shown in the app.")] = "Gmail",
    query: Annotated[
        str | None,
        Field(description="A Gmail search filter, e.g. 'label:newsletters'. Omit for all mail."),
    ] = None,
    redirect_uri: Annotated[str | None, _REDIRECT] = None,
    first_sync_days: Annotated[int | None, _FIRST_SYNC_DAYS] = None,
) -> ConnectSourceResponse:
    """Start connecting a Gmail mailbox, read-only. Returns a consent URL a person must open.

    Nothing is connected until a person opens `authorization_url` and approves Google's
    consent screen; an agent cannot do that step (invariant 9), so hand the URL to them.
    The source is created inactive now and becomes active when consent completes; then
    it polls on its own and its newsletters wait in `list_held_source_items`.
    """

    def call(c: RouteCall) -> ConnectSourceResponse:
        return routes.connect_source(
            body=ConnectSourceRequest(
                name=name,
                query=query,
                redirect_uri=consent_redirect(c.config, redirect_uri),
                first_sync_days=first_sync_days,
            ),
            conn=c.conn,
            user_id=c.user_id,
        )

    return run("connect_source", call)


def poll_source(source_id: Annotated[str, _SOURCE_ID]) -> SourceResponse:
    """Queue a poll of a connected mailbox now, instead of waiting for its schedule.

    Only queues it; new messages appear in `list_held_source_items` once a worker has
    fetched and extracted them. A paused or not-yet-connected source is a 409.
    """
    return run(
        "poll_source",
        lambda c: routes.poll_source(
            conn=c.conn, user_id=c.user_id, source_id=source_id, nudge=c.nudge
        ),
    )


def resync_source(
    source_id: Annotated[str, _SOURCE_ID],
    first_sync_days: Annotated[int, _FIRST_SYNC_DAYS],
) -> SourceResponse:
    """Search a mailbox again from `first_sync_days` ago, and keep that as its window.

    The repair for a first sync that did not reach far enough: a mailbox's window is read
    only when a search begins, so widening it alone changes nothing once a source is part
    way through one. Mail already pulled in is skipped before it is fetched, so this cannot
    duplicate anything in the backlog. Queues the poll; it does not fetch.
    """
    return run(
        "resync_source",
        lambda c: routes.resync_source(
            body=ResyncRequest(first_sync_days=first_sync_days),
            conn=c.conn,
            user_id=c.user_id,
            source_id=source_id,
            nudge=c.nudge,
        ),
    )


def set_label_sync(
    source_id: Annotated[str, _SOURCE_ID],
    remove_label: Annotated[
        str | None, Field(description="The Gmail label a message leaves when ingested.")
    ] = None,
    add_label: Annotated[
        str | None, Field(description="The Gmail label a message joins when ingested.")
    ] = None,
) -> SourceResponse:
    """Set which Gmail labels a message moves between when its owner ingests it.

    For example remove `Newsletters` and add `Completed`. Both empty turns label sync off.
    Setting labels does not widen the grant: a read-only mailbox then needs
    `reauthorize_source` before any label can move. Labels that could hide mail (TRASH,
    SPAM and other system labels besides INBOX, UNREAD, STARRED, IMPORTANT) are refused.
    """
    return run(
        "set_label_sync",
        lambda c: routes.set_label_sync(
            body=LabelSyncRequest(remove_label=remove_label, add_label=add_label),
            conn=c.conn,
            user_id=c.user_id,
            source_id=source_id,
        ),
    )


def reauthorize_source(
    source_id: Annotated[str, _SOURCE_ID],
    redirect_uri: Annotated[str | None, _REDIRECT] = None,
) -> ConnectSourceResponse:
    """Ask for permission to change a mailbox's labels. Returns a consent URL a person must open.

    Only for a Gmail source with label sync set (`set_label_sync`); anything else is a 409.
    Google's consent screen asks the person to let Motet modify labels, and only they can
    approve it.
    """

    def call(c: RouteCall) -> ConnectSourceResponse:
        return routes.reauthorize_source(
            body=ReauthorizeSourceRequest(redirect_uri=consent_redirect(c.config, redirect_uri)),
            conn=c.conn,
            user_id=c.user_id,
            source_id=source_id,
        )

    return run("reauthorize_source", call)


def disconnect_source(source_id: Annotated[str, _SOURCE_ID]) -> ActionResult:
    """Forget a mailbox's credentials and stop polling it. Everything it ingested stays.

    Reconnecting later needs a person to go through Google's consent again.
    """

    def call(c: RouteCall) -> ActionResult:
        routes.disconnect_source(conn=c.conn, user_id=c.user_id, source_id=source_id)
        return ActionResult(done=True, detail=f"Disconnected {source_id}; it no longer polls.")

    return run("disconnect_source", call)


def remove_source(source_id: Annotated[str, _SOURCE_ID]) -> ActionResult:
    """Delete a source whose consent was never finished. Anything else is refused with a 409.

    For cleaning up after an abandoned `connect_source`. A source that ever held a
    credential or pulled anything in cannot be removed, because removing it would delete
    what it ingested; use `disconnect_source` for those.
    """

    def call(c: RouteCall) -> ActionResult:
        routes.remove_source(conn=c.conn, user_id=c.user_id, source_id=source_id)
        return ActionResult(done=True, detail=f"Removed {source_id}.")

    return run("remove_source", call)


TOOLS = (
    list_sources,
    connect_source,
    poll_source,
    resync_source,
    set_label_sync,
    reauthorize_source,
    disconnect_source,
    remove_source,
)
