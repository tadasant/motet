"""The smart-episode selection corpus.

Which stories a rule picks, and in what order — the regression test for the other Phase 2
stage with no single right answer. Like dedup, it fails quietly: a briefing built from the
wrong four stories is still a briefing, and nothing errors.

**These run against the real repository query and a real Postgres**, not against a
reimplementation of the ordering in Python. That is the whole point: the selection *is* an
`ORDER BY` with a window predicate and a source-count subquery, so a corpus that
recomputed it in the harness would pass while the SQL was wrong. Cases skip without
``DATABASE_URL``, exactly as the other database-backed tests do.

A case declares stories by age, source count and read state; the harness inserts them,
applies the rule, and compares the selected titles **in order**. Order is asserted rather
than membership because the duration cap is applied by walking the selection and stopping
— so the order decides what makes the episode at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from motet_db import SmartRule, SourceKind, phase2, repo

CASES_DIR = Path(__file__).resolve().parent / "episodes"
USER = repo.OWNER_USER_ID

#: A `source_ids` entry of this form is replaced with the id of the source the harness
#: created for that kind. A case cannot hardcode an id, because ids are random.
GMAIL_PLACEHOLDER = "@gmail"


@dataclass(frozen=True)
class Story:
    title: str
    age_days: int
    sources: int
    read: bool
    source_kind: str = SourceKind.PASTE.value


@dataclass(frozen=True)
class EpisodeCase:
    name: str
    why: str
    stories: tuple[Story, ...]
    rule: Any
    expected: tuple[str, ...]

    def __str__(self) -> str:
        return self.name


def load_cases() -> list[EpisodeCase]:
    cases: list[EpisodeCase] = []
    for directory in sorted(CASES_DIR.iterdir()):
        if not directory.is_dir():
            continue
        spec = json.loads((directory / "case.json").read_text())
        cases.append(
            EpisodeCase(
                name=directory.name,
                why=spec["why"],
                stories=tuple(
                    Story(
                        title=story["title"],
                        age_days=story["age_days"],
                        sources=story["sources"],
                        read=story["read"],
                        source_kind=story.get("source_kind", SourceKind.PASTE.value),
                    )
                    for story in spec["stories"]
                ),
                rule=spec["rule"],
                expected=tuple(spec["expected"]),
            )
        )
    return cases


CASES = load_cases()


def test_the_corpus_is_not_empty() -> None:
    """Guards against a fixture path change silently reducing this file to a no-op."""
    assert CASES


def seed(db: psycopg.Connection[Any], case: EpisodeCase) -> dict[str, str]:
    """Insert the case's stories. Returns the placeholder -> source id map.

    Each story is built the way the pipeline builds one — a source item, a news item, then
    further source items merged in — so the source count the ranking reads is a real count
    of real link rows rather than a number written into a column.
    """
    gmail_id = phase2.create_source(db, user_id=USER, kind=SourceKind.GMAIL.value, name="Gmail").id

    for story in case.stories:
        source_id = (
            gmail_id if story.source_kind == SourceKind.GMAIL.value else repo.PASTE_SOURCE_ID
        )
        first = repo.insert_source_item(
            db,
            user_id=USER,
            title=story.title,
            text=f"{story.title}. A sentence of body text for this story.",
            source_id=source_id,
        )
        news_id = repo.insert_news_item(
            db,
            user_id=USER,
            title=story.title,
            summary=f"{story.title} summary.",
            source_item_id_=first.id,
        )
        for extra in range(story.sources - 1):
            item = repo.insert_source_item(
                db,
                user_id=USER,
                title=f"{story.title} ({extra})",
                text=f"{story.title}, reported again.",
                source_id=source_id,
            )
            repo.merge_source_into_news_item(
                db,
                news_item_id_=news_id,
                source_item_id_=item.id,
                title=story.title,
                summary=f"{story.title} summary.",
            )
        # Ages are set explicitly rather than by inserting in order: the window predicate
        # compares `created_at`, and a test that relied on insertion time would be a test
        # of how fast the machine is.
        db.execute(
            "UPDATE news_items SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (story.age_days, news_id),
        )
        if story.read:
            repo.set_news_item_read(db, user_id=USER, item_id=news_id, read=True)

    return {GMAIL_PLACEHOLDER: gmail_id}


def rule_for(case: EpisodeCase, sources: dict[str, str]) -> SmartRule:
    """The case's rule, with source placeholders resolved."""
    if case.rule == "manual":
        return SmartRule.manual()
    raw = dict(case.rule)
    if "source_ids" in raw:
        raw["source_ids"] = [sources.get(entry, entry) for entry in raw["source_ids"]]
    return SmartRule.from_json(raw)


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_rule_selects_what_the_case_expects(
    db: psycopg.Connection[Any], case: EpisodeCase
) -> None:
    """Which stories, and in which order.

    Order is part of the expectation because ranking is selection: the duration cap walks
    this list and stops, so a wrong order changes the episode's contents rather than just
    its running order.
    """
    sources = seed(db, case)
    selected = phase2.select_for_rule(db, USER, rule_for(case, sources))
    assert [item.title for item in selected] == list(case.expected), case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_selection_is_deterministic(db: psycopg.Connection[Any], case: EpisodeCase) -> None:
    """Same backlog, same rule, same episode.

    Every ranking is ordered down to the id. Without that tiebreak, `coverage` would return
    an arbitrary order among equally covered stories, and an episode built twice from one
    backlog could contain different stories — which is maddening to debug in production and
    is exactly what a corpus is for.
    """
    sources = seed(db, case)
    rule = rule_for(case, sources)
    first = [item.id for item in phase2.select_for_rule(db, USER, rule)]
    second = [item.id for item in phase2.select_for_rule(db, USER, rule)]
    assert first == second, case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_a_story_is_never_selected_twice(db: psycopg.Connection[Any], case: EpisodeCase) -> None:
    """A story backed by several qualifying sources appears once.

    The source filter is an `EXISTS` rather than a join for this reason — a join would
    return a three-source story three times, and the episode would speak it three times.
    """
    sources = seed(db, case)
    selected = phase2.select_for_rule(db, USER, rule_for(case, sources))
    ids = [item.id for item in selected]
    assert len(ids) == len(set(ids)), case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_unread_only_never_selects_a_read_story(
    db: psycopg.Connection[Any], case: EpisodeCase
) -> None:
    """Invariant 5 from the selection side.

    "Unread" has one definition, and both episode kinds go through this one query — so a
    rule that says unread cannot quietly include something the user already ticked off on
    the backlog screen.
    """
    sources = seed(db, case)
    rule = rule_for(case, sources)
    selected = phase2.select_for_rule(db, USER, rule)
    if rule.unread_only:
        assert all(not item.read for item in selected), case.why


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_selection_respects_max_items(db: psycopg.Connection[Any], case: EpisodeCase) -> None:
    sources = seed(db, case)
    rule = rule_for(case, sources)
    assert len(phase2.select_for_rule(db, USER, rule)) <= rule.max_items


@pytest.mark.parametrize("case", CASES, ids=str)
def test_manual_and_smart_agree_where_the_rule_is_manual(
    db: psycopg.Connection[Any], case: EpisodeCase
) -> None:
    """One selector, two episode kinds.

    ``SmartRule.manual()`` must reproduce Phase 1's ``unread_news_items`` exactly. Two
    selection paths would eventually disagree about what "unread" means, and the
    disagreement would be silent.
    """
    seed(db, case)
    by_rule = phase2.select_for_rule(db, USER, SmartRule.manual())
    by_phase1 = repo.unread_news_items(db, USER)
    assert [item.id for item in by_rule] == [item.id for item in by_phase1], case.why


# --- the rule contract itself --------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_a_rule_round_trips_through_its_snapshot(case: EpisodeCase) -> None:
    """The stored snapshot must parse back to the same rule.

    An episode's rule is a jsonb snapshot on the row, read at assembly time — possibly long
    after it was written. A rule that serialized lossily would build a different episode
    from the one the user asked for, and the schema is what makes that recoverable.
    """
    rule = rule_for(case, {GMAIL_PLACEHOLDER: "src_placeholder"})
    assert SmartRule.from_json(rule.to_json()) == rule
