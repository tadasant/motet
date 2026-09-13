"""The private podcast feed."""

from __future__ import annotations

from ...schemas import FeedInfoResponse
from ..context import current_request, routes, run


def get_feed() -> FeedInfoResponse:
    """Get the private podcast feed URL to subscribe to in a podcast app.

    The URL carries a secret token: anyone holding it can listen, so share it only with its
    owner. Creates the token on first ask.
    """
    return run(
        "get_feed",
        lambda c: routes.get_feed_info(
            request=current_request(), conn=c.conn, user_id=c.user_id, config=c.config
        ),
    )


def rotate_feed() -> FeedInfoResponse:
    """Replace the podcast feed URL. Every app subscribed to the old URL stops working.

    The answer to a leaked feed link. Only use it when the owner asks: they have to
    resubscribe on every device afterwards. Returns the new URL.
    """
    return run(
        "rotate_feed",
        lambda c: routes.rotate_feed(
            request=current_request(), conn=c.conn, user_id=c.user_id, config=c.config
        ),
    )


TOOLS = (get_feed, rotate_feed)
