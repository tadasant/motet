"""Episodes: making them, reading them, and where the listener has got to."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from ...schemas import (
    CreateEpisodeRequest,
    CreateSmartEpisodeRequest,
    EpisodeResponse,
    ListenProgressRequest,
    ListenProgressResponse,
    MarkListenedResponse,
    RenameEpisodeRequest,
    SmartRuleModel,
)
from ..context import RouteCall, routes, run

_EPISODE_ID = Field(description="The episode's id, from list_episodes.")
_TITLE = Field(
    description=(
        "The episode's title, shown in podcast apps. Omit it and the server names the "
        "episode after the day it was made."
    )
)
_MAX_DURATION = Field(
    description="The longest the episode may run, in milliseconds, e.g. 1200000 for 20 minutes."
)


def list_episodes() -> list[EpisodeResponse]:
    """List every episode, newest first, in whatever state it is in.

    `state` moves pending, assembling, scripting, rendering, ready (or failed with
    `last_error`). `listened_through_ms` is the furthest point listened to. Includes each
    episode's segments and claims, so prefer `get_episode` when you need only one.
    """
    return run("list_episodes", lambda c: routes.list_episodes(conn=c.conn, user_id=c.user_id))


def create_episode(
    max_duration_ms: Annotated[int, _MAX_DURATION],
    title: Annotated[str | None, _TITLE] = None,
    news_item_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only these news items (ids from list_news_items), read or not. Omit for "
                "every unread item."
            )
        ),
    ] = None,
    keep_in_backlog: Annotated[
        bool,
        Field(description="Listening to this episode leaves its stories unread."),
    ] = False,
) -> EpisodeResponse:
    """Make an episode from every unread news item — or only the ones named — up to a cap.

    Returns immediately in state `pending`; assembling, scripting and narration run on the
    queue over several minutes and spend inference and text-to-speech. Poll `get_episode`
    until `state` is `ready`. Use `create_smart_episode` to choose stories by rule instead.
    """
    return run(
        "create_episode",
        lambda c: routes.create_episode(
            body=CreateEpisodeRequest(
                title=title,
                max_duration_ms=max_duration_ms,
                news_item_ids=news_item_ids,
                keep_in_backlog=keep_in_backlog,
            ),
            conn=c.conn,
            user_id=c.user_id,
            nudge=c.nudge,
        ),
    )


def create_smart_episode(
    max_duration_ms: Annotated[int, _MAX_DURATION],
    title: Annotated[str | None, _TITLE] = None,
    rule: Annotated[
        SmartRuleModel | None,
        Field(description="Which stories, and in what order. Omit for everything unread."),
    ] = None,
) -> EpisodeResponse:
    """Make an episode from news items chosen by a rule: sources, a time window, a ranking.

    Like `create_episode`, it returns in `pending` and renders on the queue, spending
    inference and text-to-speech. An invalid rule is refused here with a 422 rather than
    failing minutes later.
    """
    return run(
        "create_smart_episode",
        lambda c: routes.create_smart_episode(
            body=CreateSmartEpisodeRequest(
                title=title, max_duration_ms=max_duration_ms, rule=rule or SmartRuleModel()
            ),
            conn=c.conn,
            user_id=c.user_id,
            nudge=c.nudge,
        ),
    )


def get_episode(episode_id: Annotated[str, _EPISODE_ID]) -> EpisodeResponse:
    """Get one episode with its transcript: each segment's story and each claim beside its source.

    Every claim carries the source item and character span its quote came from. Use it to
    check whether an episode is `ready`, and what it says.
    """
    return run(
        "get_episode",
        lambda c: routes.get_episode(conn=c.conn, user_id=c.user_id, episode_id=episode_id),
    )


def rename_episode(
    episode_id: Annotated[str, _EPISODE_ID],
    title: Annotated[str, Field(description="The new title, 1 to 500 characters.")],
) -> EpisodeResponse:
    """Rename an episode. Changes nothing but the title, which podcast apps show.

    An episode is named after the day it was made unless somebody said otherwise, so this
    is how one gets a name worth finding again. Safe at any point, including while the
    episode is still being made. Returns the episode. Unknown ids are a 404.
    """
    return run(
        "rename_episode",
        lambda c: routes.rename_episode(
            body=RenameEpisodeRequest(title=title),
            conn=c.conn,
            user_id=c.user_id,
            episode_id=episode_id,
        ),
    )


def set_playback_position(
    episode_id: Annotated[str, _EPISODE_ID],
    listened_through_ms: Annotated[
        int, Field(description="How far into the episode the listener has got, in milliseconds.")
    ],
) -> ListenProgressResponse:
    """Record how far the listener has got in an episode; stories passed are marked read.

    The position only moves forward: a smaller value than the stored one changes nothing.
    Every story whose segment ends before the position is marked read (invariant 5).
    Returns the stored position and how many stories were newly marked read.
    """
    return run(
        "set_playback_position",
        lambda c: routes.report_listen_progress(
            body=ListenProgressRequest(listened_through_ms=listened_through_ms),
            conn=c.conn,
            user_id=c.user_id,
            episode_id=episode_id,
        ),
    )


def mark_episode_listened(episode_id: Annotated[str, _EPISODE_ID]) -> MarkListenedResponse:
    """Mark every story in an episode read, as though it had been listened to the end.

    Marks nothing for an episode made with `keep_in_backlog`: its stories stay unread.

    There is no undo for the episode as a whole; `set_news_item_read` can mark a single
    story unread again.
    """
    return run(
        "mark_episode_listened",
        lambda c: routes.mark_episode_listened(
            conn=c.conn, user_id=c.user_id, episode_id=episode_id
        ),
    )


def get_episode_transcript(episode_id: Annotated[str, _EPISODE_ID]) -> str:
    """Get an episode's timed captions as WebVTT text, one cue per spoken claim.

    Timings exist only once the episode is rendered; before that every cue is at 00:00.
    """

    def call(c: RouteCall) -> str:
        response = routes.episode_transcript(conn=c.conn, user_id=c.user_id, episode_id=episode_id)
        return bytes(response.body).decode("utf-8")

    return run("get_episode_transcript", call)


def get_episode_chapters(episode_id: Annotated[str, _EPISODE_ID]) -> dict[str, Any]:
    """Get an episode's chapters (Podcasting 2.0 JSON): one chapter per story, with start times."""

    def call(c: RouteCall) -> dict[str, Any]:
        response = routes.episode_chapters(conn=c.conn, user_id=c.user_id, episode_id=episode_id)
        document: dict[str, Any] = json.loads(bytes(response.body))
        return document

    return run("get_episode_chapters", call)


TOOLS = (
    list_episodes,
    create_episode,
    create_smart_episode,
    get_episode,
    rename_episode,
    set_playback_position,
    mark_episode_listened,
    get_episode_transcript,
    get_episode_chapters,
)
