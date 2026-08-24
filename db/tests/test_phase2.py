"""Phase 2's storage layer, against a real Postgres.

There is no in-memory substitute worth having here, and these tests are why: the
credential table's whole design is a set of CHECK constraints and a unique index, smart
selection is an ORDER BY, highlight anchoring is a `substring` over the source text, and
read state is a partial index. A fake database would verify none of it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from motet_db import CredentialPurpose, EpisodeKind, Ranking, SmartRule, SourceKind, phase2, repo
from motet_vault import DecryptionError, LocalKeyManager

USER = repo.OWNER_USER_ID


@pytest.fixture
def key() -> LocalKeyManager:
    return LocalKeyManager(kek=hashlib.sha256(b"phase2-test-kek").digest())


def _count(db: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> int:
    """A scalar count. `repo.connect` uses a dict row factory, so rows are mappings."""
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def gmail_source(db: psycopg.Connection[Any], name: str = "Gmail") -> str:
    return phase2.create_source(db, user_id=USER, kind=SourceKind.GMAIL.value, name=name).id


# --- sources -------------------------------------------------------------------------


def test_a_gmail_source_can_be_created_and_listed(db: psycopg.Connection[Any]) -> None:
    """The `kind` CHECK was widened by migration 0003; this is that widening asserted."""
    source_id = gmail_source(db)
    listed = phase2.list_sources(db, USER)
    assert source_id in {source.id for source in listed}
    # The Phase 1 paste source is still there and still legal.
    assert repo.PASTE_SOURCE_ID in {source.id for source in listed}


def test_an_unknown_source_kind_is_refused_by_the_database(
    db: psycopg.Connection[Any],
) -> None:
    """X bookmarks are not built, and the schema says so rather than the application."""
    with pytest.raises(psycopg.errors.CheckViolation):
        phase2.create_source(db, user_id=USER, kind="x", name="X bookmarks")


def test_config_and_sync_state_are_separate(db: psycopg.Connection[Any]) -> None:
    """The user's intent and our bookmark must not be one column.

    Conflating them means "change your Gmail query" silently re-ingests the archive.
    """
    source_id = phase2.create_source(
        db, user_id=USER, kind=SourceKind.GMAIL.value, name="G", config={"query": "label:news"}
    ).id
    phase2.set_source_sync_state(db, source_id, {"cursor": "12345"})
    source = phase2.get_source(db, source_id)
    assert source is not None
    assert source.config == {"query": "label:news"}
    assert source.sync_state == {"cursor": "12345"}
    assert source.last_polled_at is not None


def test_only_active_sources_are_pollable(db: psycopg.Connection[Any]) -> None:
    active = gmail_source(db, "active")
    paused = gmail_source(db, "paused")
    phase2.set_source_active(db, paused, active=False)
    pollable = {s.id for s in phase2.list_pollable_sources(db, SourceKind.GMAIL.value)}
    assert active in pollable
    assert paused not in pollable


# --- the credential vault ------------------------------------------------------------


def test_a_credential_round_trips_through_the_vault(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    source_id = gmail_source(db)
    phase2.store_source_credential(
        db,
        key,
        user_id=USER,
        source_id_=source_id,
        provider="gmail",
        purpose=CredentialPurpose.REFRESH.value,
        secret="1//0gRefreshToken",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    loaded = phase2.load_source_credential(
        db, key, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
    )
    assert loaded == "1//0gRefreshToken"


def test_no_column_holds_the_plaintext(db: psycopg.Connection[Any], key: LocalKeyManager) -> None:
    """Invariant 8 at the storage layer, checked by looking at the whole row.

    Not "the ciphertext column has no plaintext" — *no* column does. A well-meaning change
    that added a `token` column for convenience would pass a narrower assertion.
    """
    source_id = gmail_source(db)
    secret = "1//0gVerySecretRefreshToken"
    phase2.store_source_credential(
        db,
        key,
        user_id=USER,
        source_id_=source_id,
        provider="gmail",
        purpose=CredentialPurpose.REFRESH.value,
        secret=secret,
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM source_credentials WHERE source_id = %s", (source_id,))
        row = cur.fetchone()
    assert row is not None
    rendered = " ".join(repr(value) for value in dict(row).values())
    assert secret not in rendered
    assert secret[:12] not in rendered


def test_metadata_is_readable_without_the_key(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    """ "Is this mailbox connected?" must not require decrypt permission.

    The API answers exactly that question on the sources screen, and it holds only the
    encrypt half of the vault.
    """
    source_id = gmail_source(db)
    expires = datetime.now(UTC) + timedelta(hours=1)
    phase2.store_source_credential(
        db,
        key,
        user_id=USER,
        source_id_=source_id,
        provider="gmail",
        purpose=CredentialPurpose.ACCESS.value,
        secret="ya29.access",
        scopes=["a", "b"],
        expires_at=expires,
    )
    stored = phase2.get_source_credential(
        db, source_id_=source_id, purpose=CredentialPurpose.ACCESS.value
    )
    assert stored is not None
    assert stored.scopes == ("a", "b")
    assert stored.backend == "local"
    assert not stored.expired(now=datetime.now(UTC))
    assert stored.expired(now=datetime.now(UTC) + timedelta(hours=2))


def test_reconsent_replaces_rather_than_accumulates(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    """One credential per purpose per source, so "which token is current" has one answer."""
    source_id = gmail_source(db)
    for secret in ("first", "second"):
        phase2.store_source_credential(
            db,
            key,
            user_id=USER,
            source_id_=source_id,
            provider="gmail",
            purpose=CredentialPurpose.REFRESH.value,
            secret=secret,
        )
    assert (
        _count(
            db, "SELECT count(*) AS n FROM source_credentials WHERE source_id = %s", (source_id,)
        )
        == 1
    )
    assert (
        phase2.load_source_credential(
            db, key, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
        )
        == "second"
    )


def test_a_ciphertext_moved_between_rows_does_not_decrypt(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    """The AAD binding, end to end through the database.

    This is the attack envelope encryption is *for*: copy the bytes of one account's
    credential into another's row and ask a worker to open it. The AAD is rebuilt from the
    row's own identity, so it authenticates against the wrong binding and fails.
    """
    first, second = gmail_source(db, "one"), gmail_source(db, "two")
    phase2.store_source_credential(
        db,
        key,
        user_id=USER,
        source_id_=first,
        provider="gmail",
        purpose=CredentialPurpose.REFRESH.value,
        secret="the-real-token",
    )
    phase2.store_source_credential(
        db,
        key,
        user_id=USER,
        source_id_=second,
        provider="gmail",
        purpose=CredentialPurpose.REFRESH.value,
        secret="another-token",
    )
    db.execute(
        """
        UPDATE source_credentials dst
        SET ciphertext = src.ciphertext, nonce = src.nonce, wrapped_dek = src.wrapped_dek
        FROM source_credentials src
        WHERE src.source_id = %s AND dst.source_id = %s
        """,
        (first, second),
    )
    with pytest.raises(DecryptionError):
        phase2.load_source_credential(
            db, key, source_id_=second, purpose=CredentialPurpose.REFRESH.value
        )
    # And the row it was copied *from* still works, so this is a targeted failure rather
    # than the vault having broken.
    assert (
        phase2.load_source_credential(
            db, key, source_id_=first, purpose=CredentialPurpose.REFRESH.value
        )
        == "the-real-token"
    )


def test_disconnecting_forgets_the_credentials(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    source_id = gmail_source(db)
    for purpose in (CredentialPurpose.REFRESH, CredentialPurpose.ACCESS):
        phase2.store_source_credential(
            db,
            key,
            user_id=USER,
            source_id_=source_id,
            provider="gmail",
            purpose=purpose.value,
            secret=f"token-{purpose.value}",
        )
    assert phase2.delete_source_credentials(db, source_id) == 2
    assert (
        phase2.load_source_credential(
            db, key, source_id_=source_id, purpose=CredentialPurpose.REFRESH.value
        )
        is None
    )


def test_the_schema_refuses_a_wrong_sized_nonce(db: psycopg.Connection[Any]) -> None:
    """A CHECK rather than a convention: a 12-byte GCM nonce is not negotiable."""
    source_id = gmail_source(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """
            INSERT INTO source_credentials
                (id, user_id, source_id, provider, purpose, ciphertext, nonce,
                 wrapped_dek, backend, key_name)
            VALUES ('cred_x', %s, %s, 'gmail', 'refresh', 'c', 'short', 'w', 'local', 'k')
            """,
            (USER, source_id),
        )


# --- OAuth handshake state -----------------------------------------------------------


def test_an_oauth_state_is_single_use(db: psycopg.Connection[Any]) -> None:
    """`DELETE ... RETURNING`, so a replayed callback finds nothing.

    Select-then-delete would let two concurrent callbacks both pass and both exchange the
    code — the second of which Google would reject, after the first had already stored a
    grant.
    """
    source_id = gmail_source(db)
    phase2.start_oauth(
        db,
        state="st_abc",
        user_id=USER,
        provider="gmail",
        source_id_=source_id,
        code_verifier="verifier",
        redirect_uri="https://app.example/cb",
        scopes=["scope"],
    )
    first = phase2.consume_oauth_state(db, "st_abc")
    assert first is not None
    assert first["code_verifier"] == "verifier"
    assert phase2.consume_oauth_state(db, "st_abc") is None


def test_an_expired_oauth_state_is_not_honoured(db: psycopg.Connection[Any]) -> None:
    phase2.start_oauth(
        db,
        state="st_old",
        user_id=USER,
        provider="gmail",
        source_id_=None,
        code_verifier="v",
        redirect_uri="https://app.example/cb",
        scopes=[],
        ttl_seconds=-1,
    )
    assert phase2.consume_oauth_state(db, "st_old") is None
    assert phase2.purge_expired_oauth_states(db) == 1


# --- polled source items -------------------------------------------------------------


def test_a_polled_message_is_ingested_exactly_once(db: psycopg.Connection[Any]) -> None:
    """Idempotent ingestion, which is what makes a crashed poll safe to retry."""
    source_id = gmail_source(db)
    first = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=source_id,
        external_id="msg-1",
        title="A newsletter",
        text="Body text.",
    )
    assert first is not None
    second = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=source_id,
        external_id="msg-1",
        title="A newsletter",
        text="Body text.",
    )
    assert second is None, "a re-poll must not create a second source item"
    assert phase2.source_item_exists(db, source_id_=source_id, external_id="msg-1")


def test_the_same_message_id_in_two_mailboxes_is_two_items(
    db: psycopg.Connection[Any],
) -> None:
    """The uniqueness is per source, because provider ids are only unique per account."""
    one, two = gmail_source(db, "one"), gmail_source(db, "two")
    assert phase2.insert_polled_source_item(
        db, user_id=USER, source_id_=one, external_id="m", title="T", text="B"
    )
    assert phase2.insert_polled_source_item(
        db, user_id=USER, source_id_=two, external_id="m", title="T", text="B"
    )


def test_pasted_items_are_unaffected_by_the_unique_index(
    db: psycopg.Connection[Any],
) -> None:
    """The index is partial on `external_id IS NOT NULL`; paste-in has no external id.

    Without the partial clause, a second paste would collide with the first on NULL in
    some databases — and would be a silent regression of Phase 1's only ingestion route.
    """
    for _ in range(3):
        repo.insert_source_item(db, user_id=USER, title="Pasted", text="Text")
    assert (
        _count(
            db,
            "SELECT count(*) AS n FROM source_items WHERE source_id = %s",
            (repo.PASTE_SOURCE_ID,),
        )
        == 3
    )


# --- smart selection -----------------------------------------------------------------


def seed_stories(db: psycopg.Connection[Any]) -> dict[str, str]:
    """Three stories: one old and read, one recent, one recent with two sources."""
    ids: dict[str, str] = {}
    for name, title, sources, age_days, read in (
        ("old_read", "An old story", 1, 10, True),
        ("recent", "A recent story", 1, 0, False),
        ("covered", "A widely covered story", 3, 1, False),
    ):
        first_item = repo.insert_source_item(db, user_id=USER, title=title, text=f"{title}.")
        news_id = repo.insert_news_item(
            db,
            user_id=USER,
            title=title,
            summary=f"{title} summary.",
            source_item_id_=first_item.id,
        )
        for extra in range(sources - 1):
            item = repo.insert_source_item(
                db, user_id=USER, title=f"{title} {extra}", text=f"{title} again."
            )
            repo.merge_source_into_news_item(
                db,
                news_item_id_=news_id,
                source_item_id_=item.id,
                title=title,
                summary=f"{title} summary.",
            )
        db.execute(
            "UPDATE news_items SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (age_days, news_id),
        )
        if read:
            repo.set_news_item_read(db, user_id=USER, item_id=news_id, read=True)
        ids[name] = news_id
    return ids


def test_the_manual_rule_reproduces_phase_one_behaviour(
    db: psycopg.Connection[Any],
) -> None:
    """One selector for both kinds, so "unread" cannot acquire two definitions.

    Manual is unread, no window, oldest first — which is exactly what
    `repo.unread_news_items` returns, so the two must agree exactly.
    """
    seed_stories(db)
    by_rule = phase2.select_for_rule(db, USER, SmartRule.manual())
    by_phase1 = repo.unread_news_items(db, USER)
    assert [item.id for item in by_rule] == [item.id for item in by_phase1]


def test_a_window_excludes_older_stories(db: psycopg.Connection[Any]) -> None:
    ids = seed_stories(db)
    selected = phase2.select_for_rule(
        db, USER, SmartRule(unread_only=False, window_days=2, ranking=Ranking.OLDEST_FIRST)
    )
    chosen = [item.id for item in selected]
    assert ids["old_read"] not in chosen, "a 10-day-old story is outside a 2-day window"
    assert ids["recent"] in chosen
    assert ids["covered"] in chosen


def test_unread_only_can_be_turned_off(db: psycopg.Connection[Any]) -> None:
    """A "catch me up on the week" rule wants read stories too."""
    ids = seed_stories(db)
    chosen = [
        item.id
        for item in phase2.select_for_rule(db, USER, SmartRule(unread_only=False, window_days=0))
    ]
    assert ids["old_read"] in chosen


def test_coverage_ranking_leads_with_the_most_reported_story(
    db: psycopg.Connection[Any],
) -> None:
    """The one ranking that is not just a date, and the reason it needs no model."""
    ids = seed_stories(db)
    ranked = phase2.select_for_rule(db, USER, SmartRule(window_days=0, ranking=Ranking.COVERAGE))
    assert ranked[0].id == ids["covered"]
    assert len(ranked[0].source_item_ids) == 3


def test_newest_first_is_the_reverse_of_oldest_first(db: psycopg.Connection[Any]) -> None:
    seed_stories(db)
    oldest = [i.id for i in phase2.select_for_rule(db, USER, SmartRule(window_days=0))]
    newest = [
        i.id
        for i in phase2.select_for_rule(
            db, USER, SmartRule(window_days=0, ranking=Ranking.NEWEST_FIRST)
        )
    ]
    assert newest == list(reversed(oldest))


def test_a_source_filter_selects_by_backing_source(db: psycopg.Connection[Any]) -> None:
    """ "Just my Gmail newsletters" — and a story with three sources appears once.

    An `EXISTS` rather than a join, because a join would return that story three times and
    the episode would contain the same segment three times over.
    """
    gmail_id = gmail_source(db)
    item = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=gmail_id,
        external_id="m1",
        title="From Gmail",
        text="A Gmail story.",
    )
    assert item is not None
    news_id = repo.insert_news_item(
        db, user_id=USER, title="From Gmail", summary="s", source_item_id_=item
    )
    second = phase2.insert_polled_source_item(
        db,
        user_id=USER,
        source_id_=gmail_id,
        external_id="m2",
        title="From Gmail again",
        text="More.",
    )
    assert second is not None
    repo.merge_source_into_news_item(
        db, news_item_id_=news_id, source_item_id_=second, title="From Gmail", summary="s"
    )
    seed_stories(db)  # paste-backed stories that must not be selected

    chosen = phase2.select_for_rule(db, USER, SmartRule(window_days=0, source_ids=(gmail_id,)))
    assert [c.id for c in chosen] == [news_id], "exactly once, and nothing from paste-in"


def test_selection_is_stable_across_calls(db: psycopg.Connection[Any]) -> None:
    """Every ranking is ordered down to the id, so an episode built twice is the same.

    Without the id tiebreak, `coverage` would return an arbitrary order among equally
    covered stories and two runs against one backlog could contain different stories.
    """
    seed_stories(db)
    rule = SmartRule(window_days=0, ranking=Ranking.COVERAGE)
    assert [i.id for i in phase2.select_for_rule(db, USER, rule)] == [
        i.id for i in phase2.select_for_rule(db, USER, rule)
    ]


def test_max_items_bounds_the_selection(db: psycopg.Connection[Any]) -> None:
    seed_stories(db)
    assert len(phase2.select_for_rule(db, USER, SmartRule(window_days=0, max_items=1))) == 1


# --- smart episodes ------------------------------------------------------------------


def test_a_smart_episode_stores_its_rule_as_a_snapshot(
    db: psycopg.Connection[Any],
) -> None:
    """A snapshot, not a reference: "why these stories" stays answerable."""
    rule = SmartRule(window_days=3, ranking=Ranking.COVERAGE)
    episode_id = repo.create_episode(
        db,
        user_id=USER,
        title="Morning briefing",
        max_duration_ms=600_000,
        kind=EpisodeKind.SMART,
        rule=rule.to_json(),
    )
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.kind is EpisodeKind.SMART
    assert episode.rule is not None
    assert SmartRule.from_json(episode.rule) == rule


def test_a_smart_episode_without_a_rule_is_refused_by_the_database(
    db: psycopg.Connection[Any],
) -> None:
    """A CHECK, because an episode that claimed to be smart with nothing to select by
    would fail at assembly — hours after the mistake was made."""
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """
            INSERT INTO episodes (id, user_id, title, max_duration_ms, kind)
            VALUES ('ep_bad', %s, 'No rule', 1000, 'smart')
            """,
            (USER,),
        )


def test_a_manual_episode_still_defaults(db: psycopg.Connection[Any]) -> None:
    """Every Phase 1 episode keeps meaning what it meant."""
    episode_id = repo.create_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
    episode = repo.get_episode(db, episode_id)
    assert episode is not None
    assert episode.kind is EpisodeKind.MANUAL
    assert episode.rule is None
    assert episode.listened_through_ms == 0


# --- highlights ----------------------------------------------------------------------


def story_with_source(
    db: psycopg.Connection[Any], text: str = "Acme raised $20M on Tuesday. It hired a CFO."
) -> tuple[str, str]:
    item = repo.insert_source_item(db, user_id=USER, title="Acme raised $20M", text=text)
    news_id = repo.insert_news_item(
        db, user_id=USER, title="Acme raised $20M", summary="A round.", source_item_id_=item.id
    )
    return news_id, item.id


def test_a_highlight_quotes_the_source_rather_than_the_caller(
    db: psycopg.Connection[Any],
) -> None:
    """The trust property: the quote is read out of the source item.

    In the voice case the caller is a model. A model that quoted loosely would otherwise
    write its own paraphrase into the user's highlights, where it would look verbatim.
    """
    news_id, source_id = story_with_source(db)
    saved = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
    )
    assert saved is not None
    assert saved.quote == "Acme raised $20M on Tuesday"


def test_saving_the_same_span_twice_is_idempotent(db: psycopg.Connection[Any]) -> None:
    """Voice and touch can both reach for one sentence; two rows would be a chore."""
    news_id, source_id = story_with_source(db)
    first = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
    )
    second = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
        note="worth remembering",
    )
    assert first is not None and second is not None
    assert first.id == second.id
    assert second.note == "worth remembering", "a later note should attach to the existing row"
    assert len(phase2.list_highlights(db, USER)) == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0, 0),  # empty
        (5, 3),  # inverted
        (0, 10_000),  # past the end of the source text
    ],
)
def test_a_span_that_does_not_resolve_is_not_an_anchor(
    db: psycopg.Connection[Any], start: int, end: int
) -> None:
    """The same rule `segment_claims` enforces with a CHECK.

    A highlight pointing at nothing would render as an empty quote and, worse, would look
    like a saved passage the user could no longer read.
    """
    news_id, source_id = story_with_source(db)
    assert (
        phase2.save_highlight(
            db,
            user_id=USER,
            news_item_id=news_id,
            source_item_id_=source_id,
            span_start=start,
            span_end=end,
        )
        is None
    )


def test_a_highlight_survives_its_episode(db: psycopg.Connection[Any]) -> None:
    """`episode_id` is provenance, not the anchor — so losing the episode keeps the quote.

    Cascading here would mean deleting an episode silently destroyed the highlights taken
    while listening to it, which is the outcome the anchoring decision exists to avoid.
    """
    news_id, source_id = story_with_source(db)
    episode_id = repo.create_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
    saved = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
        episode_id_=episode_id,
        anchor_ms=4_200,
    )
    assert saved is not None and saved.episode_id == episode_id
    db.execute("DELETE FROM episodes WHERE id = %s", (episode_id,))
    survivors = phase2.list_highlights(db, USER)
    assert len(survivors) == 1
    assert survivors[0].episode_id is None
    assert survivors[0].quote == "Acme raised $20M on Tuesday"


def test_a_highlight_survives_rescripting(db: psycopg.Connection[Any]) -> None:
    """The whole argument for anchoring to the source span.

    `replace_segments` deletes and re-inserts every claim, so a highlight anchored to a
    claim id would detach on any script retry. Anchored to the source span, it does not
    notice.
    """
    news_id, source_id = story_with_source(db)
    episode_id = repo.create_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
    spec = repo.SegmentSpec(
        news_item_id=news_id,
        text="Acme raised $20M on Tuesday.",
        duration_ms=5_000,
        claims=(
            repo.ClaimSpec(
                text="Acme raised $20M on Tuesday",
                source_item_id=source_id,
                span_start=0,
                span_end=27,
            ),
        ),
    )
    repo.replace_segments(db, episode_id, [spec])
    saved = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
        episode_id_=episode_id,
    )
    assert saved is not None

    # The script stage runs again: every claim row is destroyed and rewritten.
    before = {row["id"] for row in _claim_ids(db, episode_id)}
    repo.replace_segments(db, episode_id, [spec])
    after = {row["id"] for row in _claim_ids(db, episode_id)}
    assert before.isdisjoint(after), (
        "claim ids must actually have changed for this to prove anything"
    )

    still_there = phase2.list_highlights(db, USER)
    assert len(still_there) == 1
    assert still_there[0].quote == "Acme raised $20M on Tuesday"


def _claim_ids(db: psycopg.Connection[Any], episode_id: str) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT sc.id FROM segment_claims sc
            JOIN episode_segments es ON es.id = sc.segment_id
            WHERE es.episode_id = %s
            """,
            (episode_id,),
        )
        return list(cur.fetchall())


def test_highlights_can_be_filtered_by_story_and_deleted(
    db: psycopg.Connection[Any],
) -> None:
    news_id, source_id = story_with_source(db)
    other_id, _ = story_with_source(db, "A different newsletter entirely, about nothing.")
    saved = phase2.save_highlight(
        db,
        user_id=USER,
        news_item_id=news_id,
        source_item_id_=source_id,
        span_start=0,
        span_end=27,
    )
    assert saved is not None
    assert len(phase2.list_highlights(db, USER, news_item_id=news_id)) == 1
    assert phase2.list_highlights(db, USER, news_item_id=other_id) == []
    assert phase2.delete_highlight(db, user_id=USER, highlight_id_=saved.id)
    assert not phase2.delete_highlight(db, user_id=USER, highlight_id_=saved.id)


# --- read state from the audio side --------------------------------------------------


def episode_with_two_segments(db: psycopg.Connection[Any]) -> tuple[str, str, str]:
    """An episode with two rendered 5s segments, so progress has something to cross."""
    first_news, first_source = story_with_source(db, "The first story happened. It was notable.")
    second_news, second_source = story_with_source(
        db, "The second story happened too. Also notable."
    )
    episode_id = repo.create_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
    repo.replace_segments(
        db,
        episode_id,
        [
            repo.SegmentSpec(
                news_item_id=news,
                text="A sentence.",
                duration_ms=5_000,
                claims=(
                    repo.ClaimSpec(
                        text="A sentence.", source_item_id=source, span_start=0, span_end=10
                    ),
                ),
            )
            for news, source in ((first_news, first_source), (second_news, second_source))
        ],
    )
    return episode_id, first_news, second_news


def test_progress_marks_only_stories_the_listener_has_passed(
    db: psycopg.Connection[Any],
) -> None:
    """Invariant 5 from the audio side.

    The comparison is against the *end* of a segment: marking at the start would tick a
    story off on its first spoken word, which is not "I listened to this".
    """
    episode_id, first, second = episode_with_two_segments(db)

    position, marked = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=2_000
    )
    assert (position, marked) == (2_000, 0), "halfway through story one marks nothing"

    _, marked = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=5_000
    )
    assert marked == 1
    read = {item.id for item in repo.list_news_items(db, USER) if item.read}
    assert read == {first}

    _, marked = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=10_000
    )
    assert marked == 1
    read = {item.id for item in repo.list_news_items(db, USER) if item.read}
    assert read == {first, second}


def test_progress_is_monotonic(db: psycopg.Connection[Any]) -> None:
    """Invariant 4: we own the position, so scrubbing back does not un-listen.

    A client that seeks backwards is reviewing. If the position could fall, a scrub would
    un-mark a story as a side effect — and read state is meant to be one durable fact.
    """
    episode_id, first, _ = episode_with_two_segments(db)
    phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=7_000
    )
    position, marked = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=1_000
    )
    assert position == 7_000, "a smaller report must not lower the recorded position"
    assert marked == 0
    assert first in {item.id for item in repo.list_news_items(db, USER) if item.read}


def test_progress_is_idempotent(db: psycopg.Connection[Any]) -> None:
    """A client reporting the same position twice must not double-count anything."""
    episode_id, _, _ = episode_with_two_segments(db)
    _, first = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=10_000
    )
    _, second = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=10_000
    )
    assert (first, second) == (2, 0)


def test_progress_on_an_unrendered_episode_marks_nothing(
    db: psycopg.Connection[Any],
) -> None:
    """Segments with no duration are estimates, not positions.

    Before TTS runs, `duration_ms` is the assemble stage's guess. Treating a guess as a
    position would mark stories read that were never spoken.
    """
    news_id, source_id = story_with_source(db)
    episode_id = repo.create_episode(db, user_id=USER, title="Briefing", max_duration_ms=600_000)
    repo.replace_segments(
        db,
        episode_id,
        [repo.SegmentSpec(news_item_id=news_id, text="", duration_ms=0, claims=())],
    )
    _, marked = phase2.record_listen_progress(
        db, user_id=USER, episode_id_=episode_id, listened_through_ms=999_999
    )
    assert marked == 0
    assert source_id  # the source item exists; nothing about it was marked


def test_progress_on_someone_elses_episode_is_refused(
    db: psycopg.Connection[Any],
) -> None:
    """Phase 1 has one user, but every query is already scoped — so this is asserted now."""
    episode_id, _, _ = episode_with_two_segments(db)
    db.execute("INSERT INTO users (id) VALUES ('other') ON CONFLICT DO NOTHING")
    with pytest.raises(LookupError):
        phase2.record_listen_progress(
            db, user_id="other", episode_id_=episode_id, listened_through_ms=10_000
        )
