# Golden set

Newsletters in, expected news items out — the regression test for the parts of Motet that
have no single right answer.

Twenty cases today. They exist so that a change to dedup, to the script contract, or to
span resolution has to explain itself: if a case breaks, either the change is wrong or the
expectation was, and the `why` field is there so a human debugging at speed can tell which.

## Adding a case

Make a directory under `fixtures/`:

```
fixtures/0021_short_name/
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
  ],
  "script": [
    { "news_item_title": "…", "claims": ["…"] }
  ]
}
```

`script` is **optional**. Declare one where the spoken wording is the thing the case is
defending; leave it out where the case is about dedup and the copy is incidental. Pinning
every case's script would cost more maintenance than it catches.

A source item's **title is the first sentence of its file**; its text is the whole file. So
a fixture is just a newsletter — paste one in and it works. The harness needs no changes to
pick up a new directory.

> Watch out for abbreviations in the *first* sentence. "U.S. regulators open an inquiry"
> ends its first sentence at `S.`, so the title becomes `U.S`. That is the sentence
> splitter behaving as documented rather than a bug, but it makes for a confusing fixture.

## What runs in CI today

`bin/ci` runs this against the **fake** adapters, asserting properties that hold regardless
of which implementation is behind the seam:

- dedup produces exactly the expected news items, with the expected source counts
- every claim in the generated script resolves to a real source span (invariant 3)
- every news item reaches the script — no silent drops
- the script matches the one the case considers good, where the case declares one
- the pipeline is deterministic
- validated copy synthesizes to audio with a duration

## What the corpus covers

Deliberately weighted toward the ways dedup goes wrong, because that is the stage with no
single right answer:

| Shape | Cases |
|---|---|
| Merging — two, three, and seven sources on one story | `0001`, `0003`, `0016` |
| Headline variation — case, punctuation, word order | `0004`, `0005`, `0006`, `0019` |
| Not merging — unrelated stories, two stories about one company | `0002`, `0011`, `0014` |
| Realistic arrival order — interleaved merges and new stories | `0012`, `0017` |
| Text that breaks naive handling — unicode, `&`, `<`, apostrophes, colons, ragged whitespace, hard wrapping, long bodies | `0007`, `0008`, `0013`, `0015`, `0018`, `0020` |
| Degenerate — a single newsletter, an all-numbers story | `0009`, `0010` |

## What does not run in CI, and why

**Quality.** Whether a briefing is worth listening to is not a pass/fail assertion, and it
needs real model calls — slow, priced, and nondeterministic, which is exactly what
invariant 7 keeps out of CI. Scoring the corpus against the real adapters is a separate
job, run deliberately.

Note that the fake deduper collapses titles that differ only in case, punctuation, and word
order. Seeing through genuinely different *wording* is the real adapter's job, so a fixture
that needs it belongs with that adapter, not here.
