# Backlog — design crit

**Status:** three passes on `proto/local-ux`, 2026-09-12. Pass 1 and 2 were written before
any code changed; pass 3 after. Before/after screenshots are in `proto/screenshots/`
(`backlog-before-*`, `backlog-after-*`). Branding is out of scope — neutral greys only;
hierarchy is spacing, weight and grouping.

## Pass 1 — information architecture

### What the screen is for

One job: **decide what gets briefed, and make the briefing.** Everything else on it is in
service of that decision — knowing what came in, what it costs to process, and whether
the machine behind it is running.

### Who arrives, and with what question

| Arrives from | Question | What they need first |
|---|---|---|
| Sources, after a sync | "What came in?" | the held list, newest first, with size |
| The sidebar badge (`Backlog 11`) | "What's waiting on me?" | the held count and the one button that clears it |
| Paste in, just pasted | "Where did it go?" | the processing state of *that* item |
| Habit, before a walk | "Make me an episode" | the primary action, above the fold |
| The episode screen | "What did I hear / what's left?" | the unread count, and read state |

Four of the five want something above the fold. The current page puts one of them there.

### What competes for attention now (before, 1400×900)

Three stacked panels of three different shapes, in the order the *data* flows rather
than the order the *person* asks:

1. **Held** — a bordered card containing a `<table>` with a `details` link inside every
   title, a disabled "Ingest now" beside the heading, a hint paragraph, and a size column.
   Eleven rows fill the whole first viewport.
2. **Processing** — a second bordered card, a different shape (an `<ul>` of
   head/badge/sentence blocks), listing **the same eleven items again** as "QUEUED ·
   Queued 2h ago. A worker is draining the queue now." That is false — they are held, not
   queued, and nothing is draining them — and it is the API counting held items as pending
   (`09-followups.md`, "Held items count as `pending`"). The screen repeats the list and
   contradicts itself in the repeat. ~1,300px of that.
3. **Processed** — a bare `<h3>`, a hint sentence, then the cap input and "Make an
   episode" as a plain `.row`, then 41 unbordered `<li>`s, each: bold title, a
   full-width bordered "Mark read" button, the whole summary paragraph, and a hint line
   `1 source: <underlined source title> · ni_9babfdd07bf1`.

The page is **11,284px tall**. The primary action sits at ~2,300px. Nothing is sticky.

### What is missing

- **No way to find anything.** 41 items, no search, no sort, no filter; read items stay
  in the same list at the same size, only greyed.
- **No bulk read/unread.** One button per row, one request per click; there is no "mark
  everything above this read" for the case that actually happens (you listened to an
  episode but the position sync missed).
- **The primary action is buried** under two panels, and the cap input beside it is a
  co-equal control — a number field with a label as prominent as the button.
- **Counts are in three places** and disagree in kind: the sidebar badge says 11 (held),
  the Held heading says 11, Processing says "11 on the way in" (the same 11, misreported),
  and "41 unread of 41" is a sentence a hundred pixels lower.
- **No state on a news row** other than a greyed title. Age is not shown. Source count is
  a sentence.
- **Empty states**: Held and Processing vanish entirely rather than saying "nothing
  waiting", so the *absence* of the panel is the only signal — and the Processing card is
  the same shape whether one item is stuck or a worker is dead.

### What is wrong with the copy

- **"Processed"** is pipeline jargon for "your stories". **"Ingested, awaiting
  processing"** is two pipeline words for "waiting for you". Neither says what to *do*.
- **"An episode takes everything unread, oldest first, until it hits the cap."** True and
  useful, but it is the explanation of a button, sitting as a paragraph above a different
  heading.
- **"~28k chars → dedup at low effort, each"** is a note to the engineer, not the owner.
- **`· ni_9babfdd07bf1`** on every row: an id nobody types anywhere on this screen.
- **"A worker is draining the queue now"** on eleven items nobody has queued.
- **"Nothing here yet. Paste a newsletter in."** is fine, and is the only empty state.

### What is inconsistent with the other screens

- **Episodes** is a shelf: one row per episode, `title · meta` left, a pill and a `Play`
  button right, listened rows folded into a `<details>`. **Sources** is cards with status
  pills and stat tiles. The backlog is a table *and* an `<ul>` of cards *and* a bare list,
  with no pills, no folded section, and two competing button styles (`.primary` exists in
  `styles.css` and is not used here).
- Episodes derives a state per row and shows it; the backlog's only row state is
  `.read strong { color: muted }`.
- Sources already labels the held count **"Waiting for you"** on its stat tile — the
  backlog calls the same number "Ingested, awaiting processing".

### Problems, ranked

1. **The primary action is ~2,300px down and not visually primary.** The one thing the
   screen exists for is the hardest to reach.
2. **The same eleven items are listed twice**, and the second listing says something
   false about them ("queued", "a worker is draining the queue now").
3. **No search, sort, or fold for 41+ items**; read items never leave the way.
4. **No bulk action on news items**, and the per-row "Mark read" button is the loudest
   element on every row.
5. **The counts live in four places** with three vocabularies (held / on the way in /
   unread of), and none is a summary.
6. **Rows do not scan**: the news row is a paragraph with an id; the held row has a
   `details` link wedged into its title cell; age is absent from one and a clock time on
   the other.
7. **Inline lifecycle detail pushes the list around**: opening it inserts a ~600px card
   into the flow, in two different places (a table row, an `<li>`), and the list jumps.
8. **Processing is a card whether it has something to say or not**, and its vocabulary
   ("stuck", "on the way in") does not match anything else on the page.

## Pass 2 — layout proposals

Constraints taken as given: one primary action with the cap folded in; a clear split
between **Waiting for you** and **Ready to brief**, with **Read** folded; a sticky summary
header carrying counts and the action; rows that scan; bulk selection that works the same
way in both lists; search-as-you-type; sort; the lifecycle in a drawer, not the flow; the
worker as a strip, not a card; empty states per section.

### Option A — two columns

```
┌ Backlog ───────────────────────────────────────────────────────────────────────┐
│ 11 waiting · 41 ready · 0 read                          [ Make an episode ][20m▾]│
│ ● Worker idle · last pass 3m ago                                                │
├────────────────────────────────────┬───────────────────────────────────────────┤
│ WAITING FOR YOU (11)   [☐ all]     │ READY TO BRIEF (41)          [☐ all]       │
│ ☐ OpenAI Releases GPT-6…  28k 2h  │ ☐ Lenny's Community Wisdom…   1 src  2h    │
│ ☐ Nvidia's Sales Chief…    2k 2h  │ ☐ The Rise of the Forward…    1 src  2h    │
│ ☐ …                                │ ☐ …                                        │
│                                    │ ▸ Read (0)                                 │
└────────────────────────────────────┴───────────────────────────────────────────┘
         [ 3 selected · ~40k ] [ Ingest 3 ] [ clear ]     ← contextual bar
```

Buys: both questions answered in one viewport; the flow (left → right) is the pipeline.
Costs: at 1000px each column is ~24rem and long titles wrap to three lines; the content
area is capped at 62rem so the split is cramped even at 1400; two select-alls side by side
invite selecting in both, which the action bar then has to arbitrate; the held list is
usually empty or short and the news list is long, so the columns are almost never
balanced. It also makes search and sort per-column or ambiguous.

### Option B — one column, two sections, sticky summary  ← picked

```
┌ Backlog ───────────────────────────────────────────────────────────────────────┐
│ 11 waiting for you · 41 ready to brief · 0 read        [ Make an episode ][20m▾]│ sticky
│ ● Worker idle · last pass 3m ago                                                │ strip
│ [🔍 Search titles…                                 ]   Sort [ Newest ▾ ]         │
├────────────────────────────────────────────────────────────────────────────────┤
│ WAITING FOR YOU  11                                                  [☐ all]   │
│ ☐  OpenAI Releases GPT-6 Astra Model…        Gmail · 28k         2h ago         │
│ ☐  Nvidia's Sales Chief, Anthropic's Mega…   Gmail · 2k          2h ago         │
│ …                                                                               │
├────────────────────────────────────────────────────────────────────────────────┤
│ READY TO BRIEF  41                                                   [☐ all]   │
│ ☐  Lenny's Community Wisdom: AI basketball… 1 source           2h ago  [read]  │
│ ☐  The Rise of the Forward Deployed…        1 source           2h ago  [read]  │
│    ▾ expanded: summary, then the source titles as links → drawer                │
│ …                                                                               │
│ ▸ READ  0                                              (folded <details>)      │
└────────────────────────────────────────────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
                 │ 3 selected · ~40k chars   [Ingest 3] [✕] │  ← fixed, bottom-centre
                 └──────────────────────────────────────────┘
                                          ┌──────── drawer ──────────┐
                                          │ ✕  OpenAI Releases GPT-6 │
                                          │ ① Pulled in  …           │
                                          │ ② Processed  …           │
                                          │ ③ News item  …           │
                                          └──────────────────────────┘
```

Buys: the summary + action are always on screen (sticky under the top bar), which fixes
#1 outright; the two sections use one row component and one selection model, so "select
all" and the bar mean the same thing in both; search and sort are one control over both
lists; the held section collapses to a single line when empty; the drawer keeps the list
still. Costs: Waiting for you still comes first, so with many held items the news list
starts below the fold — accepted, because held is the section that needs a *decision*,
and the counts in the header say what is below. Selection is one list at a time (selecting
in one clears the other): a bar with two verbs is a bar the person has to read.

**Picked B.** A wants a wider page than the shell gives and balances two lists that are
never the same length; B fixes the ranked list with the least new shape and matches the
Episodes shelf (rows, folded section) rather than inventing a third layout.

### Row anatomy

```
[☐] Title, one line, ellipsis ..........  meta (source · size | N sources)   age   [row action]
```

- Held: `Gmail · 28k`, age from `received_at`, title click → drawer.
- News: `2 sources`, age from `created_at`, title click → inline expansion (summary and
  the source titles; each source opens the drawer). A quiet per-row **Mark read** on
  hover/focus; the bulk bar for more than one.
- Read rows: muted, folded under **Read (N)**, same row component; bar offers **Mark N
  unread**.

### Status strip

One line under the header, no border: `● Worker running · last pass 12s ago` /
`○ Worker idle · last ran 2h ago` / `○ No worker has ever run` / `? Couldn't check`.
When items are in flight (pending or failed, **excluding held ids** — the API counts them
as pending, followup) it becomes `3 processing · 1 failed  ▾` and expands to the existing
Processing panel unchanged, open by default only when something failed.

### Empty states

- Waiting for you, 0: one line — "Nothing waiting for you. Items your sources pull in land
  here until you ingest them."
- Ready to brief, 0 with read items: "Everything's been briefed. Mark something unread to
  brief it again."
- Nothing at all and nothing in flight: "Nothing here yet. Paste a newsletter in."
- Search with no hits: "No titles match “…”."

## Pass 3 — after

Screenshots: `backlog-after-1400x900.png`, `backlog-after-1400x900-full.png`,
`backlog-after-1000x800.png`, `backlog-after-selection.png` (three held items picked, the
bar showing), `backlog-after-detail.png` (a news row expanded, its source open in the
drawer), `backlog-after-read-fold.png`, `backlog-after-search.png`, `backlog-after-cap.png`.

The page went from **11,284px to 2,958px** tall with the same data, and the primary action
is on screen at every scroll position.

### The ranked list, re-read

1. ✅ **Primary action buried and not primary.** "Make an episode" is the one `.primary`
   button on the screen, in a sticky header under the top bar, with the cap folded into a
   "20 min" toggle and a popover. The counts sit beside it in one sentence.
2. ✅ **Same eleven items listed twice, the second time falsely.** The Processing card is
   gone from the flow; the status strip subtracts held ids before counting anything as
   processing, so it reads `● Worker running · last pass 3s ago` and nothing else. The
   *API* still reports them as pending — that is the `09-followups.md` entry, unchanged —
   so this is a client-side subtraction until the route grows a `held` status
   (`Backlog.test.tsx` pins it).
3. ✅ **No search, sort, or fold.** One search box over both lists (title contains,
   as-you-type; section counts read `2 of 11` while filtering), one sort (newest /
   oldest) over both, and read items folded under **Read (N)** as a `<details>`, muted.
4. ✅ **No bulk action; per-row button loudest thing on the row.** Checkbox column in
   both lists, select-all per section, one selection at a time, and a fixed bottom bar
   with the list's verb: `Ingest N` for held, `Mark N read` / `Mark N unread` for news. The
   per-row `Mark read` / `Details` is a quiet link shown on hover or focus.
5. ✅ **Counts in four places, three vocabularies.** One sentence in the header — `11
   waiting for you · 41 ready to brief · 0 read` — and each section head repeats only its
   own count. "Waiting for you" is the label the Sources tile already used. The sidebar
   badge is App's and still counts unsettled ingestion items; see leftovers.
6. ✅ **Rows do not scan.** One `ItemRow` for both lists: checkbox · title (one line,
   ellipsis) · meta (`Gmail · 28k` or `2 sources`) · age (`2h ago`) · action. Summary and
   the source links are behind a click, and the `ni_…` id is gone.
7. ✅ **Inline lifecycle detail pushes the list around.** `SourceItemDetail` is unchanged
   in content and now lives in a right-edge drawer (`backlog/Drawer.tsx`), opened from a
   held title, a held row's `Details`, or a source link under an expanded news row.
   Escape closes it; "Show in backlog" still scrolls and highlights the row, opening the
   Read fold if that is where it is. The only in-flow expansion left is a news row's own
   summary, which is short.
8. ⚠️ **Processing is a card whether or not it has something to say.** It is a one-line
   strip now, and when items are in flight it becomes `3 processing · 1 failed ▾` with the
   existing `Processing` panel behind a disclosure (open by default only on a failure).
   The panel's own vocabulary ("on the way in", "stuck") is untouched, because the App
   tests pin those strings and the panel is shared with the paste flow — so the strip and
   its expansion still use two registers. Leftover.

### Things noticed after, not on the list

- The sticky header bleeds to `main`'s padding edge, so its bottom rule is 1.5rem wider
  than the row rules on each side. Deliberate — it reads as a band under the top bar —
  but a reviewer might want it flush.
- Selection is grey (`--hover`), not the accent, to stay inside "neutral greys only";
  the checkbox is what says a row is selected, the tint only groups them.
- Ages are against the browser clock at render, not the server's `now`: the App only
  polls `/v1/processing` while something is pending, so an age read off that stamp would
  freeze at page load. The worker-freshness decision still uses the server clock.
- `useHeld` goes through `api.heldSourceItems()` and `apiPost('/v1/source-items/integrate')`,
  which closes the "Held.tsx uses a raw fetch" follow-up; `SourceItemDetail` still fetches
  raw.
