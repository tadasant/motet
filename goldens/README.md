# Golden set

Newsletters in, expected news items out — the regression test for the parts of Motet that
have no single right answer.

## Adding a case

Make a directory under `fixtures/`:

```
fixtures/0003_short_name/
├── sources/
│   ├── 01_whatever.md      # a newsletter, verbatim — applied in filename order
│   └── 02_whatever.md
└── expected.json
```

`expected.json`:

```json
{
  "why": "One sentence on what this case is defending. Read by a human debugging a failure.",
  "news_items": [
    { "title": "…", "source_count": 2 }
  ]
}
```

A source item's **title is the first sentence of its file**; its text is the whole file. So
a fixture is just a newsletter — paste one in and it works. The harness needs no changes to
pick up a new directory.

## What runs in CI today

`bin/ci` runs this against the **fake** adapters, asserting properties that hold regardless
of which implementation is behind the seam:

- dedup produces exactly the expected news items, with the expected source counts
- every claim in the generated script resolves to a real source span (invariant 3)
- every news item reaches the script — no silent drops
- the pipeline is deterministic
- validated copy synthesizes to audio with a duration

## What does not run in CI, and why

**Quality.** Whether a briefing is worth listening to is not a pass/fail assertion, and it
needs real model calls — slow, priced, and nondeterministic, which is exactly what
invariant 7 keeps out of CI. Scoring the corpus against the real adapters is a separate
job, run deliberately.

**The real corpus.** Two placeholder cases are in here. The plan calls for ~20 real
newsletters plus a script considered good; that lands with the pipeline it tests. The two
present cases exist so the harness is wired, exercised, and failing-if-broken from day one
— not so that anyone mistakes them for coverage.

Note that the fake deduper collapses titles that differ only in case, punctuation, and word
order. Seeing through genuinely different *wording* is the real adapter's job, so a fixture
that needs it belongs with that adapter, not here.
