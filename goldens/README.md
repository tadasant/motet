# Golden set

Newsletters in, expected output out — the regression test for the parts of Motet that have
no single right answer and that fail *quietly*.

Three corpora, one per stage with that property:

| Corpus | Directory | What it defends |
|---|---|---|
| **Dedup and script** | `fixtures/` | two newsletters about one funding round become one news item, and every claim resolves to a real span |
| **Gmail extraction** | `gmail/` | a newsletter's prose survives and its machinery does not — preheaders, footers, tracking pixels, encoding lies |
| **Smart-episode selection** | `episodes/` | which stories a rule picks, and in what order |

Each runs against the fakes and a real Postgres where the stage needs one. No corpus calls
a vendor.

Twenty cases today. They exist so that a change to dedup, to the script contract, or to
span resolution has to explain itself: if a case breaks, either the change is wrong or the
expectation was, and the `why` field is there so a human debugging at speed can tell which.

## Adding a dedup/script case

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

## Adding a Gmail extraction case

Make a directory under `gmail/` holding a complete RFC 822 message and what it should
produce:

```
gmail/0007_short_name/
├── message.eml       # a real message, headers and all — exactly what Gmail returns
└── expected.json
```

`expected.json`:

```json
{
  "why": "One sentence on what this case is defending.",
  "title": "The decoded Subject, exactly",
  "text_contains": ["a sentence that must survive"],
  "text_excludes": ["boilerplate that must not"],
  "no_c1_controls": true
}
```

Or, for a message that must be **refused** — a receipt, a notification, anything that is
not a newsletter:

```json
{ "why": "…", "refused": true }
```

Assertions are on *content* rather than on an exact body: pinning the whole extracted text
would break on every whitespace tweak and would say nothing about whether extraction was
right. What a case pins is the pair of properties that matter — the prose survived, and the
machinery did not.

### What this corpus covers

| Shape | Cases |
|---|---|
| The canonical newsletter — multipart/alternative, quoted-printable, hidden preheader, tracking pixel, footer | `0001` |
| HTML-only in base64, table-based layout, entities, tracking hrefs | `0002` |
| windows-1252 declared as iso-8859-1, in subject *and* body | `0003` |
| A forward wrapping the newsletter, with a `text/*` attachment | `0004` |
| Not a newsletter at all — refused rather than ingested | `0005` |
| "Unsubscribe" in the masthead, where cutting would eat the body | `0006` |

## Adding a smart-episode case

Make a directory under `episodes/` with one file:

```
episodes/0009_short_name/
└── case.json
```

```json
{
  "why": "One sentence on what this case is defending.",
  "stories": [
    { "title": "…", "age_days": 2, "sources": 3, "read": false, "source_kind": "gmail" }
  ],
  "rule": { "ranking": "coverage", "window_days": 2 },
  "expected": ["…"]
}
```

`rule` is either the string `"manual"` or a rule object. `source_ids` may contain the
placeholder `"@gmail"`, which the harness swaps for the id of the Gmail source it created —
a case cannot hardcode an id, because ids are random.

**`expected` is ordered.** Ranking is selection as much as presentation: the duration cap is
applied by walking the selection and stopping, so a wrong order changes what is *in* the
episode rather than just the running order.

These cases run against the real repository query and a real Postgres, not a
reimplementation of the ordering in the harness — the selection *is* an `ORDER BY` with a
window predicate and a source-count subquery, so a corpus that recomputed it in Python
would pass while the SQL was wrong.

### What this corpus covers

| Shape | Cases |
|---|---|
| Manual reproduced as a rule, agreeing with Phase 1's own query | `0001` |
| The window, excluding stale stories | `0002` |
| `coverage` ranking — the most independently reported story leads | `0003` |
| `newest_first` — a morning briefing rather than a backlog drain | `0004` |
| `unread_only: false` — "catch me up on the week" | `0005` |
| `max_items`, truncating the ranking rather than sampling | `0006` |
| A source filter, with a multi-source story appearing exactly once | `0007` |
| A rule that matches nothing, selecting nothing | `0008` |
