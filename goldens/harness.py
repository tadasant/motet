"""Loader for the golden set.

A case is a directory under ``fixtures/``:

    fixtures/<NNNN_name>/
        sources/*.md    -- newsletters, applied in filename order
        expected.json   -- what the pipeline should produce, plus a `why`

A source item's ``title`` is the first sentence of its file and its ``text`` is the whole
file. Titles are derived rather than declared so a fixture is a plausible paste-in — the
same thing a user would actually put in the box — instead of a pre-parsed structure that
quietly does the pipeline's job for it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from motet_inference import SourceItem, first_sentence

CASES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class ExpectedNewsItem:
    title: str
    source_count: int


@dataclass(frozen=True)
class GoldenCase:
    name: str
    why: str
    sources: tuple[SourceItem, ...]
    expected: tuple[ExpectedNewsItem, ...]

    def __str__(self) -> str:
        return self.name


def load_case(directory: Path) -> GoldenCase:
    spec = json.loads((directory / "expected.json").read_text())
    source_paths = sorted((directory / "sources").glob("*.md"))
    if not source_paths:
        raise ValueError(f"golden case {directory.name} has no sources")

    sources = tuple(
        SourceItem(id=path.stem, title=first_sentence(text), text=text)
        for path, text in ((p, p.read_text()) for p in source_paths)
    )
    expected = tuple(
        ExpectedNewsItem(title=item["title"], source_count=item["source_count"])
        for item in spec["news_items"]
    )
    return GoldenCase(name=directory.name, why=spec["why"], sources=sources, expected=expected)


def load_cases() -> Iterator[GoldenCase]:
    for directory in sorted(CASES_DIR.iterdir()):
        if directory.is_dir():
            yield load_case(directory)
