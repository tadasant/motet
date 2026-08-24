"""The smart-episode rule: filter, window, duration, ranking.

A manual episode says "everything unread, oldest first, until the cap". A smart episode
says the same thing with the four knobs the design specifies turned to something else. So
this is deliberately a *narrowing* of the manual behaviour rather than a second selection
path — manual is the rule with every default left alone, which is what keeps one selector
in the repository instead of two that drift.

**Ranking is deterministic and model-free, on purpose.** Every option here is computable
from the rows: how old a story is, and how many independent sources covered it. Ranking
with a model is a real Phase 3 feature, and it would put an LLM call in the assemble stage
— a stage that currently costs nothing and cannot fail. `coverage` is the interesting one:
a story three newsletters all wrote about is, empirically, the story of the day, and it
costs a `count(*)` to know.

**Rules are stored as a snapshot on the episode**, not referenced from a rule table. An
episode is a historical artifact and "why does this contain these stories" has to stay
answerable after the rule is edited — a foreign key to a mutable row would let the answer
change under a listener who already heard it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class Ranking(StrEnum):
    """The order stories are considered in, and therefore what survives the cap.

    Order is *selection* as much as presentation: the duration cap is applied by walking
    this order and stopping, so the ranking decides what makes the episode at all.
    """

    #: What manual episodes do. Empties a backlog, because the thing you have been
    #: ignoring longest is the thing you get first.
    OLDEST_FIRST = "oldest_first"
    #: A morning briefing: what happened since you last listened.
    NEWEST_FIRST = "newest_first"
    #: Most independently covered first. A story three newsletters all ran is the one
    #: worth leading with, and the source count is the only signal for that which needs
    #: no model.
    COVERAGE = "coverage"


#: How far back a smart episode reaches by default. Two days rather than one, so a
#: briefing built on a Monday still contains the weekend.
DEFAULT_WINDOW_DAYS: Final = 2

#: Ceiling on the window a rule may ask for. Not arbitrary: the same bound the dedup
#: window has, for the same reason — everything selected is eventually passed in-prompt,
#: and an unbounded window is an unbounded bill.
MAX_WINDOW_DAYS: Final = 30

#: Most stories one episode may be assembled from, before the duration cap is applied.
#: The cap is the real limit; this bounds the work done to discover it.
MAX_ITEMS: Final = 100


class RuleError(ValueError):
    """A rule could not be understood, so no episode was built from it."""


@dataclass(frozen=True)
class SmartRule:
    """Filter, window, duration, ranking — the whole shape, with manual as the default.

    ``max_duration_ms`` is deliberately **not** here: it lives on the episode row, which
    is where the pipeline already reads it from. Two copies of a duration cap is one copy
    too many, and the one that got out of date would be the one somebody trusted.
    """

    #: Only consider unread stories. Almost always true — a briefing that re-reads what
    #: you have heard is not a briefing — but a "catch me up on the week" rule wants it
    #: off, so it is a knob rather than an assumption.
    unread_only: bool = True
    #: Restrict to stories backed by at least one source item from these sources. Empty
    #: means every source. This is what "just my Gmail newsletters" is.
    source_ids: tuple[str, ...] = ()
    window_days: int = DEFAULT_WINDOW_DAYS
    ranking: Ranking = Ranking.OLDEST_FIRST
    max_items: int = MAX_ITEMS

    @classmethod
    def manual(cls) -> SmartRule:
        """What a Phase 1 manual episode has always done, stated as a rule.

        ``window_days=0`` means no window at all: "all unread" reaches back forever, and
        that is the behaviour a manual episode has today. Expressing it here is what lets
        one selector serve both kinds.
        """
        return cls(unread_only=True, window_days=0, ranking=Ranking.OLDEST_FIRST)

    @classmethod
    def from_json(cls, raw: Any) -> SmartRule:
        """Parse a rule from a request body or a stored snapshot.

        Every field is validated, and an unknown key is an error rather than something
        ignored: a rule with a misspelled ``rankingg`` that silently selected by age would
        produce a plausible episode and no clue that the knob did nothing.
        """
        if not isinstance(raw, dict):
            raise RuleError(f"a rule must be an object, got {type(raw).__name__}")

        known = {"unread_only", "source_ids", "window_days", "ranking", "max_items"}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise RuleError(f"unknown rule field(s): {', '.join(unknown)}")

        window_days = _int(raw, "window_days", DEFAULT_WINDOW_DAYS)
        if not 0 <= window_days <= MAX_WINDOW_DAYS:
            raise RuleError(f"window_days must be between 0 and {MAX_WINDOW_DAYS}")

        max_items = _int(raw, "max_items", MAX_ITEMS)
        if not 1 <= max_items <= MAX_ITEMS:
            raise RuleError(f"max_items must be between 1 and {MAX_ITEMS}")

        raw_ranking = raw.get("ranking", Ranking.OLDEST_FIRST.value)
        try:
            ranking = Ranking(raw_ranking)
        except ValueError:
            raise RuleError(
                f"ranking must be one of {', '.join(r.value for r in Ranking)}, got {raw_ranking!r}"
            ) from None

        raw_sources = raw.get("source_ids", [])
        if not isinstance(raw_sources, list) or any(not isinstance(s, str) for s in raw_sources):
            raise RuleError("source_ids must be a list of strings")

        unread_only = raw.get("unread_only", True)
        if not isinstance(unread_only, bool):
            raise RuleError("unread_only must be a boolean")

        return cls(
            unread_only=unread_only,
            # Sorted and deduplicated so that two rules meaning the same thing serialize
            # to the same snapshot — which is what makes an episode's stored rule
            # comparable, and the golden set deterministic.
            source_ids=tuple(sorted(set(raw_sources))),
            window_days=window_days,
            ranking=ranking,
            max_items=max_items,
        )

    def to_json(self) -> dict[str, Any]:
        """The snapshot stored on the episode. Every field explicit, no defaults implied."""
        return {
            "unread_only": self.unread_only,
            "source_ids": list(self.source_ids),
            "window_days": self.window_days,
            "ranking": self.ranking.value,
            "max_items": self.max_items,
        }


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    # `bool` is an `int` in Python, and `window_days: true` should be an error rather
    # than a one-day window.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleError(f"{key} must be an integer")
    return value
