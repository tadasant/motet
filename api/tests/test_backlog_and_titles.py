"""Three product changes that meet on the same screens, and the rules they rest on.

* An episode is named after the day it was made unless somebody says otherwise, and the
  name can be changed afterwards.
* A backlog row is named after its *source* when there is one source, and after dedup when
  there are several — with the provenance behind it readable in one request.

The rules are asserted over a real Postgres through ``TestClient``, so the dependency
graph, the response models and the generated contract are all exercised as a client meets
them. Dedup runs on the deterministic fake (invariant 7), which is enough: what is under
test here is which *stored* string a route chooses, not what a model writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.deps import reset_store
from motet_api.main import display_title, episode_title
from motet_db import repo

TOKEN = "test-api-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(
    db: psycopg.Connection[Any],
    _migrated: str,
    object_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setenv("MOTET_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", _migrated)
    reset_store()
    with TestClient(app) as started:
        yield started
    reset_store()


def one_story(db: psycopg.Connection[Any], *, sources: list[tuple[str, str]]) -> str:
    """A news item made from ``sources``, written the way dedup writes one.

    The first source creates the story; the rest merge into it, which is what leaves the
    news item wearing a title that is nobody's subject line.
    """
    first = repo.insert_source_item(
        db, user_id=repo.OWNER_USER_ID, title=sources[0][0], text=sources[0][1]
    )
    news_item_id = repo.insert_news_item(
        db,
        user_id=repo.OWNER_USER_ID,
        title="Acme raises $20M, say several",
        summary="Two newsletters covered the round.",
        source_item_id_=first.id,
    )
    for title, text in sources[1:]:
        merged = repo.insert_source_item(db, user_id=repo.OWNER_USER_ID, title=title, text=text)
        repo.merge_source_into_news_item(
            db,
            news_item_id_=news_item_id,
            source_item_id_=merged.id,
            title="Acme raises $20M, say several",
            summary="Two newsletters covered the round.",
        )
    db.commit()
    return news_item_id


class TestWhatABacklogRowIsCalled:
    def test_one_source_is_that_sources_own_title_verbatim(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        one_story(db, sources=[("Platformer: the retention inquiry", "A regulator asked.")])

        (item,) = api.get("/v1/news-items", headers=AUTH).json()

        assert item["display_title"] == "Platformer: the retention inquiry"
        # The stored title is still reported: it is what dedup wrote, and the episode and
        # the show notes still speak it.
        assert item["title"] == "Acme raises $20M, say several"

    def test_several_sources_wear_the_title_dedup_wrote(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        one_story(
            db,
            sources=[
                ("The Download — Tuesday", "Acme raised twenty million."),
                ("Import AI", "Acme's round closed."),
            ],
        )

        (item,) = api.get("/v1/news-items", headers=AUTH).json()

        assert item["display_title"] == "Acme raises $20M, say several"
        # The affordance a client counts: how many write-ups are behind the one line.
        assert len(item["sources"]) == 2

    def test_a_source_with_no_title_falls_back_rather_than_showing_nothing(self) -> None:
        """An extractor that found no subject line has given us nothing to show, and an
        empty row is worse than a paraphrase."""
        assert display_title("Dedup's title", [""]) == "Dedup's title"
        assert display_title("Dedup's title", ["   "]) == "Dedup's title"
        assert display_title("Dedup's title", []) == "Dedup's title"


class TestTheProvenanceBehindARow:
    def test_a_merged_story_names_every_source_in_the_order_it_accumulated(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        news_item_id = one_story(
            db,
            sources=[
                ("The Download — Tuesday", "Acme raised twenty million dollars on Tuesday."),
                ("Import AI", "Acme's Series A closed this week."),
            ],
        )

        detail = api.get(f"/v1/news-items/{news_item_id}", headers=AUTH)

        assert detail.status_code == 200
        body = detail.json()
        assert body["display_title"] == "Acme raises $20M, say several"
        assert body["summary"] == "Two newsletters covered the round."
        assert [source["title"] for source in body["sources"]] == [
            "The Download — Tuesday",
            "Import AI",
        ]
        # Which one started the story and which merged in — the thing a reader needs to
        # make sense of a title that is nobody's subject line.
        assert [source["position"] for source in body["sources"]] == [0, 1]
        first = body["sources"][0]
        assert first["preview"].startswith("Acme raised twenty million")
        assert first["chars"] == len("Acme raised twenty million dollars on Tuesday.")
        assert first["source_kind"] == "paste"

    def test_the_preview_is_bounded_and_the_whole_text_is_not_sent(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        """A provenance screen must not become several hundred kilobytes of mailbox."""
        long_text = "Acme. " * 2_000
        news_item_id = one_story(db, sources=[("A long newsletter", long_text)])

        (source,) = api.get(f"/v1/news-items/{news_item_id}", headers=AUTH).json()["sources"]

        assert len(source["preview"]) == repo.PREVIEW_CHARS
        assert source["chars"] == len(long_text)

    def test_another_users_story_is_a_404_like_one_that_does_not_exist(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        db.execute("INSERT INTO users (id, email) VALUES ('other', NULL)")
        other_source = repo.insert_source_item(
            db, user_id="other", title="Private mail", text="Not yours."
        )
        theirs = repo.insert_news_item(
            db,
            user_id="other",
            title="Theirs",
            summary="Theirs.",
            source_item_id_=other_source.id,
        )
        db.commit()

        assert api.get(f"/v1/news-items/{theirs}", headers=AUTH).status_code == 404
        assert api.get("/v1/news-items/ni_nope", headers=AUTH).status_code == 404


class TestWhatAnEpisodeIsCalled:
    def test_an_episode_nobody_named_is_named_after_the_day(self, api: TestClient) -> None:
        created = api.post("/v1/episodes", json={"max_duration_ms": 600_000}, headers=AUTH)

        assert created.status_code == 201
        assert created.json()["title"] == f"{datetime.now(UTC):%Y-%m-%d}"

    def test_a_blank_title_is_the_same_as_none(self, api: TestClient) -> None:
        created = api.post(
            "/v1/episodes", json={"title": "   ", "max_duration_ms": 600_000}, headers=AUTH
        )

        assert created.json()["title"] == f"{datetime.now(UTC):%Y-%m-%d}"

    def test_a_title_somebody_typed_is_kept(self, api: TestClient) -> None:
        created = api.post(
            "/v1/episodes",
            json={"title": "  Morning briefing  ", "max_duration_ms": 600_000},
            headers=AUTH,
        )

        assert created.json()["title"] == "Morning briefing"

    def test_a_smart_episode_is_named_the_same_way(self, api: TestClient) -> None:
        created = api.post("/v1/episodes/smart", json={"max_duration_ms": 600_000}, headers=AUTH)

        assert created.status_code == 201
        assert created.json()["title"] == f"{datetime.now(UTC):%Y-%m-%d}"

    def test_the_default_is_composed_in_one_place(self) -> None:
        """Three clients used to build their own; this is the only one that remains."""
        fixed = datetime(2026, 9, 20, 23, 30, tzinfo=UTC)
        assert episode_title(None, now=fixed) == "2026-09-20"
        assert episode_title("", now=fixed) == "2026-09-20"
        assert episode_title(" Something ", now=fixed) == "Something"


class TestRenamingAnEpisode:
    def test_a_rename_sticks_and_changes_nothing_else(self, api: TestClient) -> None:
        episode = api.post("/v1/episodes", json={"max_duration_ms": 600_000}, headers=AUTH).json()

        renamed = api.put(
            f"/v1/episodes/{episode['id']}/title",
            json={"title": "  The Tuesday walk  "},
            headers=AUTH,
        )

        assert renamed.status_code == 200
        assert renamed.json()["title"] == "The Tuesday walk"
        assert renamed.json()["state"] == episode["state"]
        assert renamed.json()["max_duration_ms"] == episode["max_duration_ms"]
        assert api.get(f"/v1/episodes/{episode['id']}", headers=AUTH).json()["title"] == (
            "The Tuesday walk"
        )

    def test_renaming_does_not_move_the_row_the_build_report_ages_against(
        self, api: TestClient, db: psycopg.Connection[Any], _migrated: str
    ) -> None:
        """``episodes.updated_at`` is when the *build* stopped, and a failed episode's
        report is shown for ten minutes after that. A rename that touched it would reopen
        the failure panel on an episode that gave up days ago."""
        episode = api.post("/v1/episodes", json={"max_duration_ms": 600_000}, headers=AUTH).json()
        db.execute(
            "UPDATE episodes SET state = 'failed', last_error = 'gave up', "
            "updated_at = now() - interval '3 days' WHERE id = %s",
            (episode["id"],),
        )
        db.commit()
        before = db.execute(
            "SELECT updated_at FROM episodes WHERE id = %s", (episode["id"],)
        ).fetchone()
        assert before is not None

        api.put(f"/v1/episodes/{episode['id']}/title", json={"title": "Renamed"}, headers=AUTH)

        after = db.execute(
            "SELECT updated_at, title FROM episodes WHERE id = %s", (episode["id"],)
        ).fetchone()
        assert after is not None
        assert after["title"] == "Renamed"
        assert after["updated_at"] == before["updated_at"]
        # And the consequence the column exists for: a settled build stays settled.
        assert (
            api.get(f"/v1/episodes/{episode['id']}", headers=AUTH).json()["build_progress"] is None
        )

    def test_an_empty_title_is_refused_rather_than_stored(self, api: TestClient) -> None:
        episode = api.post("/v1/episodes", json={"max_duration_ms": 600_000}, headers=AUTH).json()

        refused = api.put(f"/v1/episodes/{episode['id']}/title", json={"title": ""}, headers=AUTH)

        assert refused.status_code == 422
        assert (
            api.get(f"/v1/episodes/{episode['id']}", headers=AUTH).json()["title"]
            == (episode["title"])
        )

    def test_somebody_elses_episode_is_a_404(
        self, api: TestClient, db: psycopg.Connection[Any]
    ) -> None:
        db.execute("INSERT INTO users (id, email) VALUES ('other', NULL)")
        theirs = repo.create_episode(db, user_id="other", title="Theirs", max_duration_ms=600_000)
        db.commit()

        refused = api.put(f"/v1/episodes/{theirs}/title", json={"title": "Mine"}, headers=AUTH)
        assert refused.status_code == 404
        row = db.execute("SELECT title FROM episodes WHERE id = %s", (theirs,)).fetchone()
        assert row is not None and row["title"] == "Theirs"
