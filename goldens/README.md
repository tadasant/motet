# Golden set

Newsletters in, expected output out — the regression test for the parts of Motet that have
no single right answer and that fail *quietly*.

Four corpora, one per stage with that property:

| Corpus | Directory | What it defends |
|---|---|---|
| **Dedup and script** | `fixtures/` | two newsletters about one funding round become one news item, and every claim resolves to a real span |
| **Gmail extraction** | `gmail/` | a newsletter's prose survives and its machinery does not — preheaders, footers, tracking pixels, encoding lies |
| **Smart-episode selection** | `episodes/` | which stories a rule picks, and in what order |
| **Grounding evidence** | `grounding/` | what the gate is shown when it judges a claim, and what it does with it |

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
- the grounding gate is shown the source item behind a claim's citation, is shown no other
  source item, and still refuses a claim the source does not support

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
that needs it belongs with that adapter, not here — which is where motet#41's second half
lives: `inference/tests/test_adapters.py::TestTheSecondLook` and, end to end through a real
Postgres, `workers/tests/test_pipeline.py::TestThreeWriteUpsOfOneStory`. Both drive the real
`ClaudeIntegrator` over a scripted `FakeLlmClient`. Adding a fixture here whose sources need
semantic matching would only test the fake's title normalizer.

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

## Adding a grounding case

Make a directory under `grounding/` holding the source items and the claims to put in
front of the gate:

```
grounding/0006_short_name/
├── sources/
│   └── 01_whatever.md      # a source item, verbatim; its id is the file stem
└── case.json
```

```json
{
  "why": "One sentence on what this case is defending.",
  "claims": [
    {
      "spoken": "what the briefing would say",
      "cited": "a span copied verbatim out of the source",
      "source": "01_whatever",
      "expected": "supported"
    }
  ],
  "evidence_contains": ["185"],
  "evidence_excludes": ["Lakeside"],
  "source_blocks": 1
}
```

`source` is optional and defaults to the first file. `cited` is located with the pipeline's
own `locate_quote`, so a quotation that has drifted from its source fails the case rather
than becoming a different span — and it must be **one line**, because the harness reads the
prompt back a line at a time.

The last three fields are optional and are what a case says *beyond* the verdict:

- **`evidence_contains` / `evidence_excludes`** are checked against the `SOURCE` blocks
  only — never the whole prompt, because a claim's own spoken text contains the very
  figure a case is asking about. The excludes are the load-bearing half: they are how a
  case states a **bound** on the evidence, which no verdict can express.
- **`source_blocks`** pins how many distinct blocks the prompt carried. It is the cost
  half, and it is the half that fails quietly — sending one newsletter once per claim
  would change no verdict and would triple the input to the most expensive stage there is.

### How it is judged, and why that is honest

The stand-in model (`NumericJudge` in `grounding_harness.py`) answers exactly one question,
from exactly what the prompt showed it: *does every number in the spoken text appear in the
`SOURCE` block this claim names?* That is the first entry in the grounding prompt's own
list of what is not supported, and it is the failure motet#45's staging false positive was
misread as. It reads the source out of the prompt and out of nothing the harness knows, so
a verdict here is a statement about the evidence the validator **assembled**. A claim whose
block it cannot find is unsupported — fail closed, the same rule the validator follows for
a claim it got no verdict for.

It judges numbers, not entailment, so this corpus says nothing about whether a real model
reads a paraphrase correctly. That is the same separate, slower job the other corpora defer.

### What this corpus covers

| Shape | Cases |
|---|---|
| Support a paragraph outside the cited span — motet#45's own instance | `0001` |
| A figure the source states nowhere, still refused | `0002` |
| A figure another source item states, still refused — the widening stops at one item | `0003` |
| Support further away than the evidence window, still refused — the bound, pinned | `0004` |
| Three claims of one story travelling as one source block — the cost property | `0005` |

**The refusing cases are not optional.** A validator that got weaker fails silently, so a
corpus carrying only `0001` would pass just as well against a gate that had stopped
checking anything.

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
