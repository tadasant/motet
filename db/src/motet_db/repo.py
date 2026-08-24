"""Every SQL statement Motet runs, in one place.

The API and the workers share this module rather than each writing their own queries.
That is not only DRY: read state, the dedup window, and the episode state machine are all
things two callers could plausibly define slightly differently, and a slight difference
in the definition of "unread" is exactly how invariant 5 stops being true.

**Transactions belong to the caller.** Nothing here commits. A worker that integrates a
source item writes the news item, the link row, and the source item's new state in one
transaction, and it can only do that if this module stays out of transaction management.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row

from .ids import claim_id, episode_id, feed_token, news_item_id, segment_id, source_item_id
from .models import (
    EpisodeKind,
    EpisodeState,
    SourceItemState,
    StoredClaim,
    StoredEpisode,
    StoredNewsItem,
    StoredSegment,
    StoredSourceItem,
)

#: Phase 1 is one hardcoded account, seeded by migration 0002. Not configuration: "one
#: user, no signup" is the design. Signup and multi-tenancy are Phase 3, and every query
#: here already takes a ``user_id`` so that arriving is a routing change, not a rewrite.
OWNER_USER_ID: Final = "motet-owner"

#: The single Phase 1 source. Gmail and X become further rows in Phase 2.
PASTE_SOURCE_ID: Final = "src_paste"

#: How far back the dedup window reaches for *read* items. Unread items are always in the
#: window regardless of age — they are what an episode gets built from — but a story you
#: already heard about should still absorb a follow-up newsletter for a couple of days,
#: rather than reappearing as a fresh item.
WINDOW_DAYS: Final = 3

#: A hard ceiling on the window, because it is passed in-prompt. A day of news is roughly
#: 4.5k tokens (which is why there is no vector store); this bounds the pathological case
#: where a long silence is followed by a large paste.
WINDOW_MAX_ITEMS: Final = 200


def connect(database_url: str) -> psycopg.Connection[Any]:
    """Open a connection with the row factory the rest of this module assumes."""
    return psycopg.connect(database_url, row_factory=dict_row)


# --- source items ------------------------------------------------------------------


def insert_source_item(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    title: str,
    text: str,
    source_id: str = PASTE_SOURCE_ID,
) -> StoredSourceItem:
    row = _one(
        conn,
        """
        INSERT INTO source_items (id, user_id, source_id, title, text)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, user_id, source_id, title, text, state, created_at
        """,
        (source_item_id(), user_id, source_id, title, text),
    )
    return _source_item(row)


def get_source_item(conn: psycopg.Connection[Any], item_id: str) -> StoredSourceItem | None:
    row = _maybe_one(
        conn,
        "SELECT id, user_id, source_id, title, text, state, created_at "
        "FROM source_items WHERE id = %s",
        (item_id,),
    )
    return _source_item(row) if row else None


def load_source_items(
    conn: psycopg.Connection[Any], item_ids: Sequence[str]
) -> dict[str, StoredSourceItem]:
    """Fetch many source items by id, for resolving the spans a script cites."""
    if not item_ids:
        return {}
    rows = _all(
        conn,
        "SELECT id, user_id, source_id, title, text, state, created_at "
        "FROM source_items WHERE id = ANY(%s)",
        (list(item_ids),),
    )
    return {row["id"]: _source_item(row) for row in rows}


def mark_source_item(
    conn: psycopg.Connection[Any],
    item_id: str,
    state: SourceItemState,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE source_items
        SET state = %s,
            last_error = %s,
            integrated_at = CASE WHEN %s = 'integrated' THEN now() ELSE integrated_at END
        WHERE id = %s
        """,
        (state.value, error, state.value, item_id),
    )


# --- news items --------------------------------------------------------------------


def news_item_window(
    conn: psycopg.Connection[Any],
    user_id: str,
    *,
    now: datetime | None = None,
    max_items: int = WINDOW_MAX_ITEMS,
) -> list[StoredNewsItem]:
    """The stories a newly arrived source item is integrated against.

    Everything unread, plus anything recent enough that a follow-up should still merge
    into it. Ordered oldest-first, so the rendering of this window is stable across calls
    in one ingestion run — which is what makes the prompt cache pay off (the window is the
    large, stable prefix; the source item being integrated is not).

    **The cap keeps the most RECENT items, then re-sorts them oldest-first.** Applying the
    limit to an oldest-first ordering would do the opposite of what the cap is for: once a
    user has more than ``max_items`` qualifying stories, the window would contain only the
    stale end of the backlog, so a follow-up newsletter about something from this morning
    would find nothing to merge into and duplicate the story. That is precisely the
    large-paste case the cap exists to bound.
    """
    cutoff = (now or _now(conn)) - timedelta(days=WINDOW_DAYS)
    rows = _all(
        conn,
        """
        SELECT * FROM (
            SELECT id, user_id, title, summary, read_at, created_at
            FROM news_items
            WHERE user_id = %s AND (read_at IS NULL OR created_at >= %s)
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        ) recent
        ORDER BY created_at, id
        """,
        (user_id, cutoff, max_items),
    )
    return _attach_sources(conn, rows)


def list_news_items(
    conn: psycopg.Connection[Any], user_id: str, *, unread_only: bool = False
) -> list[StoredNewsItem]:
    """The backlog screen, newest first."""
    rows = _all(
        conn,
        f"""
        SELECT id, user_id, title, summary, read_at, created_at
        FROM news_items
        WHERE user_id = %s {"AND read_at IS NULL" if unread_only else ""}
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    )
    return _attach_sources(conn, rows)


def unread_news_items(conn: psycopg.Connection[Any], user_id: str) -> list[StoredNewsItem]:
    """What a manual "all unread" episode is assembled from, oldest first.

    Oldest first on purpose: a briefing that opens with the thing you have been ignoring
    longest is the one that empties a backlog.
    """
    rows = _all(
        conn,
        """
        SELECT id, user_id, title, summary, read_at, created_at
        FROM news_items
        WHERE user_id = %s AND read_at IS NULL
        ORDER BY created_at, id
        """,
        (user_id,),
    )
    return _attach_sources(conn, rows)


def load_news_items(
    conn: psycopg.Connection[Any], item_ids: Sequence[str]
) -> dict[str, StoredNewsItem]:
    """Fetch specific news items by id.

    Exists so that rendering an episode does not have to read the whole backlog to find
    three titles — which, on the list endpoint, would be one full scan per episode.
    """
    if not item_ids:
        return {}
    rows = _all(
        conn,
        """
        SELECT id, user_id, title, summary, read_at, created_at
        FROM news_items WHERE id = ANY(%s)
        """,
        (list(item_ids),),
    )
    return {item.id: item for item in _attach_sources(conn, rows)}


def insert_news_item(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    title: str,
    summary: str,
    source_item_id_: str,
) -> str:
    """Create a news item from a source item that matched nothing in the window."""
    new_id = news_item_id()
    conn.execute(
        "INSERT INTO news_items (id, user_id, title, summary) VALUES (%s, %s, %s, %s)",
        (new_id, user_id, title, summary),
    )
    conn.execute(
        "INSERT INTO news_item_sources (news_item_id, source_item_id, position) VALUES (%s, %s, 0)",
        (new_id, source_item_id_),
    )
    return new_id


def merge_source_into_news_item(
    conn: psycopg.Connection[Any],
    *,
    news_item_id_: str,
    source_item_id_: str,
    title: str,
    summary: str,
) -> None:
    """Fold a source item into an existing story, refreshing its title and summary.

    The `UNIQUE` on ``news_item_sources.source_item_id`` means a second attempt to file
    the same source item raises rather than double-counting it.
    """
    position = _one(
        conn,
        "SELECT coalesce(max(position) + 1, 0) AS next FROM news_item_sources "
        "WHERE news_item_id = %s",
        (news_item_id_,),
    )["next"]
    conn.execute(
        "INSERT INTO news_item_sources (news_item_id, source_item_id, position) "
        "VALUES (%s, %s, %s)",
        (news_item_id_, source_item_id_, position),
    )
    conn.execute(
        "UPDATE news_items SET title = %s, summary = %s, updated_at = now() WHERE id = %s",
        (title, summary, news_item_id_),
    )


def set_news_item_read(
    conn: psycopg.Connection[Any], *, user_id: str, item_id: str, read: bool
) -> StoredNewsItem | None:
    """Mark one news item read or unread.

    Invariant 5 in one statement: this is the *only* place read state is written, so
    marking something read on the backlog screen and having listened past it in an
    episode are the same fact rather than two facts that drift.
    """
    row = _maybe_one(
        conn,
        """
        UPDATE news_items
        SET read_at = CASE WHEN %s THEN coalesce(read_at, now()) ELSE NULL END,
            updated_at = now()
        WHERE id = %s AND user_id = %s
        RETURNING id, user_id, title, summary, read_at, created_at
        """,
        (read, item_id, user_id),
    )
    if row is None:
        return None
    return _attach_sources(conn, [row])[0]


def mark_news_items_read(
    conn: psycopg.Connection[Any], *, user_id: str, item_ids: Sequence[str]
) -> int:
    """Mark a set of news items read — what "I listened to this episode" means."""
    if not item_ids:
        return 0
    result = conn.execute(
        """
        UPDATE news_items
        SET read_at = coalesce(read_at, now()), updated_at = now()
        WHERE user_id = %s AND id = ANY(%s) AND read_at IS NULL
        """,
        (user_id, list(item_ids)),
    )
    return result.rowcount


# --- episodes ----------------------------------------------------------------------


def create_episode(
    conn: psycopg.Connection[Any],
    *,
    user_id: str,
    title: str,
    max_duration_ms: int,
    kind: EpisodeKind = EpisodeKind.MANUAL,
    rule: dict[str, Any] | None = None,
) -> str:
    """Create an episode. ``rule`` is a snapshot, stored on the row and never referenced.

    A smart episode must carry one — the schema has a CHECK saying so, because an episode
    that claimed to be smart with nothing to select by would fail at assembly, hours after
    the mistake was made.
    """
    new_id = episode_id()
    conn.execute(
        """
        INSERT INTO episodes (id, user_id, title, max_duration_ms, kind, rule)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            new_id,
            user_id,
            title,
            max_duration_ms,
            kind.value,
            json.dumps(rule) if rule is not None else None,
        ),
    )
    return new_id


def get_episode(
    conn: psycopg.Connection[Any], episode_id_: str, *, user_id: str | None = None
) -> StoredEpisode | None:
    row = _maybe_one(
        conn,
        """
        SELECT id, user_id, title, state, kind, rule, max_duration_ms, duration_ms,
               audio_key, audio_bytes, audio_media_type, last_error, listened_through_ms,
               created_at, published_at
        FROM episodes
        WHERE id = %s AND (%s::text IS NULL OR user_id = %s)
        """,
        (episode_id_, user_id, user_id),
    )
    if row is None:
        return None
    return _episode(row, _segments_for(conn, [row["id"]]).get(row["id"], ()))


def list_episodes(conn: psycopg.Connection[Any], user_id: str) -> list[StoredEpisode]:
    rows = _all(
        conn,
        """
        SELECT id, user_id, title, state, kind, rule, max_duration_ms, duration_ms,
               audio_key, audio_bytes, audio_media_type, last_error, listened_through_ms,
               created_at, published_at
        FROM episodes
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    )
    segments = _segments_for(conn, [row["id"] for row in rows])
    return [_episode(row, segments.get(row["id"], ())) for row in rows]


def list_published_episodes(conn: psycopg.Connection[Any], user_id: str) -> list[StoredEpisode]:
    """What goes in the RSS feed: episodes whose audio actually exists, newest first."""
    rows = _all(
        conn,
        """
        SELECT id, user_id, title, state, kind, rule, max_duration_ms, duration_ms,
               audio_key, audio_bytes, audio_media_type, last_error, listened_through_ms,
               created_at, published_at
        FROM episodes
        WHERE user_id = %s AND state = 'ready' AND audio_key IS NOT NULL
        ORDER BY published_at DESC, id DESC
        """,
        (user_id,),
    )
    segments = _segments_for(conn, [row["id"] for row in rows])
    return [_episode(row, segments.get(row["id"], ())) for row in rows]


def set_episode_state(
    conn: psycopg.Connection[Any],
    episode_id_: str,
    state: EpisodeState,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE episodes SET state = %s, last_error = %s, updated_at = now() WHERE id = %s",
        (state.value, error, episode_id_),
    )


@dataclass(frozen=True)
class ClaimSpec:
    """A claim on its way into the database — spoken text plus its verbatim evidence."""

    text: str
    source_item_id: str
    span_start: int
    span_end: int


@dataclass(frozen=True)
class SegmentSpec:
    news_item_id: str
    text: str
    duration_ms: int
    claims: tuple[ClaimSpec, ...]


def replace_segments(
    conn: psycopg.Connection[Any], episode_id_: str, segments: Sequence[SegmentSpec]
) -> None:
    """Write an episode's segments, replacing whatever was there.

    Replacing rather than appending is what makes the script stage idempotent: a retry
    after a partial failure produces one set of segments, not two. ``start_ms`` is
    accumulated here from each segment's duration, so playback offsets are ours
    (invariant 4) and are consistent with the order the segments will actually be spoken.
    """
    conn.execute("DELETE FROM episode_segments WHERE episode_id = %s", (episode_id_,))
    start_ms = 0
    for position, spec in enumerate(segments):
        seg_id = segment_id()
        conn.execute(
            """
            INSERT INTO episode_segments
                (id, episode_id, news_item_id, position, text, start_ms, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                seg_id,
                episode_id_,
                spec.news_item_id,
                position,
                spec.text,
                start_ms,
                spec.duration_ms,
            ),
        )
        for claim_position, claim in enumerate(spec.claims):
            conn.execute(
                """
                INSERT INTO segment_claims
                    (id, segment_id, position, text, source_item_id, span_start, span_end)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id(),
                    seg_id,
                    claim_position,
                    claim.text,
                    claim.source_item_id,
                    claim.span_start,
                    claim.span_end,
                ),
            )
        start_ms += spec.duration_ms


def set_segment_durations(
    conn: psycopg.Connection[Any], episode_id_: str, durations_ms: Sequence[int]
) -> int:
    """Replace estimated segment durations with the ones TTS actually produced.

    Returns the episode's total duration. Estimates are what the duration cap is applied
    against before any audio exists; these are the real numbers, and everything a client
    seeks against is derived from them.
    """
    rows = _all(
        conn,
        "SELECT id FROM episode_segments WHERE episode_id = %s ORDER BY position",
        (episode_id_,),
    )
    if len(rows) != len(durations_ms):
        raise ValueError(
            f"episode {episode_id_} has {len(rows)} segments but {len(durations_ms)} "
            "durations were measured"
        )
    start_ms = 0
    for row, duration_ms in zip(rows, durations_ms, strict=True):
        conn.execute(
            "UPDATE episode_segments SET start_ms = %s, duration_ms = %s WHERE id = %s",
            (start_ms, duration_ms, row["id"]),
        )
        start_ms += duration_ms
    return start_ms


def publish_episode(
    conn: psycopg.Connection[Any],
    episode_id_: str,
    *,
    audio_key: str,
    audio_bytes: int,
    audio_media_type: str,
    duration_ms: int,
) -> None:
    """The last step: audio exists, so the episode enters the feed."""
    conn.execute(
        """
        UPDATE episodes
        SET state = 'ready', audio_key = %s, audio_bytes = %s, audio_media_type = %s,
            duration_ms = %s, last_error = NULL, published_at = coalesce(published_at, now()),
            updated_at = now()
        WHERE id = %s
        """,
        (audio_key, audio_bytes, audio_media_type, duration_ms, episode_id_),
    )


# --- feed tokens -------------------------------------------------------------------


def active_feed_token(conn: psycopg.Connection[Any], user_id: str) -> str | None:
    row = _maybe_one(
        conn,
        "SELECT token FROM feed_tokens WHERE user_id = %s AND revoked_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    return row["token"] if row else None


def ensure_feed_token(conn: psycopg.Connection[Any], user_id: str) -> str:
    """The user's feed token, minted on first ask.

    Not created by a migration: a secret that shipped in version control, or that were
    identical across every environment, would not be a secret.
    """
    existing = active_feed_token(conn, user_id)
    if existing is not None:
        return existing
    token = feed_token()
    conn.execute("INSERT INTO feed_tokens (token, user_id) VALUES (%s, %s)", (token, user_id))
    return token


def rotate_feed_token(conn: psycopg.Connection[Any], user_id: str) -> str:
    """Revoke every current token and mint a new one.

    This breaks existing subscriptions, which is the point — it is the response to a leak,
    and a feed URL cannot be un-shared any other way.
    """
    conn.execute(
        "UPDATE feed_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )
    token = feed_token()
    conn.execute("INSERT INTO feed_tokens (token, user_id) VALUES (%s, %s)", (token, user_id))
    return token


def user_for_feed_token(conn: psycopg.Connection[Any], token: str) -> str | None:
    """Resolve a feed token to its owner, or None if it is unknown or revoked."""
    if not token:
        return None
    row = _maybe_one(
        conn,
        "SELECT user_id FROM feed_tokens WHERE token = %s AND revoked_at IS NULL",
        (token,),
    )
    return row["user_id"] if row else None


# --- row plumbing ------------------------------------------------------------------


def _now(conn: psycopg.Connection[Any]) -> datetime:
    """The *database's* clock, not the worker's.

    The window cutoff is compared against `created_at` values the database wrote. Mixing
    in a second clock makes the window silently wrong by whatever the two machines
    disagree by, which shows up as a story that failed to merge and nothing else.
    """
    value = _one(conn, "SELECT now() AS now", ())["now"]
    assert isinstance(value, datetime)
    return value


def _one(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = _maybe_one(conn, sql, params)
    if row is None:
        raise LookupError(f"expected exactly one row from: {sql.strip().splitlines()[0]}")
    return row


def _maybe_one(
    conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _source_item(row: dict[str, Any]) -> StoredSourceItem:
    return StoredSourceItem(
        id=row["id"],
        user_id=row["user_id"],
        source_id=row["source_id"],
        title=row["title"],
        text=row["text"],
        state=SourceItemState(row["state"]),
        created_at=row["created_at"],
    )


def _attach_sources(
    conn: psycopg.Connection[Any], rows: Sequence[dict[str, Any]]
) -> list[StoredNewsItem]:
    """One extra query for the whole page, rather than one per news item."""
    ids = [row["id"] for row in rows]
    links: dict[str, list[str]] = {item_id: [] for item_id in ids}
    if ids:
        for link in _all(
            conn,
            "SELECT news_item_id, source_item_id FROM news_item_sources "
            "WHERE news_item_id = ANY(%s) ORDER BY news_item_id, position",
            (ids,),
        ):
            links[link["news_item_id"]].append(link["source_item_id"])
    return [
        StoredNewsItem(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            summary=row["summary"],
            read_at=row["read_at"],
            created_at=row["created_at"],
            source_item_ids=tuple(links[row["id"]]),
        )
        for row in rows
    ]


def _segments_for(
    conn: psycopg.Connection[Any], episode_ids: Iterable[str]
) -> dict[str, tuple[StoredSegment, ...]]:
    ids = list(episode_ids)
    if not ids:
        return {}
    segment_rows = _all(
        conn,
        """
        SELECT id, episode_id, news_item_id, position, text, start_ms, duration_ms
        FROM episode_segments WHERE episode_id = ANY(%s) ORDER BY episode_id, position
        """,
        (ids,),
    )
    claim_rows = _all(
        conn,
        """
        SELECT id, segment_id, position, text, source_item_id, span_start, span_end,
               start_ms, duration_ms
        FROM segment_claims WHERE segment_id = ANY(%s) ORDER BY segment_id, position
        """,
        ([row["id"] for row in segment_rows],),
    )
    claims: dict[str, list[StoredClaim]] = {row["id"]: [] for row in segment_rows}
    for row in claim_rows:
        claims[row["segment_id"]].append(
            StoredClaim(
                id=row["id"],
                position=row["position"],
                text=row["text"],
                source_item_id=row["source_item_id"],
                span_start=row["span_start"],
                span_end=row["span_end"],
                start_ms=row["start_ms"],
                duration_ms=row["duration_ms"],
            )
        )

    by_episode: dict[str, list[StoredSegment]] = {episode: [] for episode in ids}
    for row in segment_rows:
        by_episode[row["episode_id"]].append(
            StoredSegment(
                id=row["id"],
                news_item_id=row["news_item_id"],
                position=row["position"],
                text=row["text"],
                start_ms=row["start_ms"],
                duration_ms=row["duration_ms"],
                claims=tuple(claims[row["id"]]),
            )
        )
    return {episode: tuple(segments) for episode, segments in by_episode.items()}


def _episode(row: dict[str, Any], segments: tuple[StoredSegment, ...]) -> StoredEpisode:
    return StoredEpisode(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        state=EpisodeState(row["state"]),
        kind=EpisodeKind(row["kind"]),
        rule=dict(row["rule"]) if row["rule"] else None,
        max_duration_ms=row["max_duration_ms"],
        duration_ms=row["duration_ms"],
        audio_key=row["audio_key"],
        audio_bytes=row["audio_bytes"],
        audio_media_type=row["audio_media_type"],
        last_error=row["last_error"],
        listened_through_ms=row["listened_through_ms"],
        created_at=row["created_at"],
        published_at=row["published_at"],
        segments=segments,
    )
