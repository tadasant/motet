-- Connecting a source does the free work at once; inference waits for an explicit
-- "ingest now" (motet#91). Three additions, each a column on an existing table used the
-- way that table is already used.
--
-- **Held needs no column, and gets none.** A polled item that `handle_extract` stored and
-- did not queue is `state = 'pending'` with no `integrate` job, and that combination *is*
-- the held state — `repo._HELD_WHERE` spells it once, for the listing, the claim and the
-- dismiss. A `held` flag beside it would be a second definition of one fact.
--
-- 1. `source_items.received_at` — when the message says it was sent, which is what a
--    person means by "when did this arrive". `created_at` is when a worker stored it, and
--    a 60-day first sync stored every message within the same minute. Backfilled from
--    `created_at` for rows that already exist, because the `Date:` header was read and
--    discarded at extraction and the raw bytes are not kept: an existing row's true
--    date is unrecoverable, and "stored at" is the honest stand-in. A paste takes the
--    default, which is the same transaction timestamp `created_at` takes.
--
--    Added nullable, backfilled, then defaulted and made NOT NULL — in that order,
--    deliberately: `ADD COLUMN ... DEFAULT now()` would evaluate `now()` once, at
--    migration time, and stamp every existing row with the moment this ran.
--
-- 2. `dismissed` joins the source item states. A newsletter somebody never wants briefed
--    has to be discardable without paying for it, or the held list only grows. A state
--    rather than a DELETE because the row is what keeps the message from coming back: the
--    `(source_id, external_id)` index is how a re-poll knows it has seen a message, and a
--    deleted row would be re-fetched and held again on the next bounded resync.
--
-- 3. Dedup's decision, per source item, on the link row that records the outcome it
--    produced. `news_item_sources.position` has always been the one durable trace of
--    *which* thing dedup did (0 created the story, anything higher merged into it); these
--    columns are *why*. `relation`, `reason`, `candidate_id` and `model` are the first
--    pass's answer; `basis` says which step the outcome rests on — the first pass, the
--    second look, or the handler's title backstop. `decided_title` and `decided_summary`
--    are the news item's copy as this decision left it: the news item's own columns are
--    rewritten by every later merge, so without them "what did dedup write for this item"
--    has no answer. All nullable, and NULL means "not recorded": every link written
--    before this migration, and any integrator that returns no decision.
--
--    No CHECK on `relation` or `basis`, and no foreign key on `candidate_id`. These are
--    descriptive, and the candidate is whatever id the model named — including one that
--    is not in the window, which is precisely the answer worth being able to read later.
--    A constraint that failed the write would fail the integrate job, and a story that
--    never reaches the backlog is a worse outcome than a decision recorded oddly.
--
-- Safe against existing rows: the backfill touches every `source_items` row once, which
-- in a single-user deployment is a few hundred; the CHECK is re-validated over the same
-- rows; the link-table columns are nullable with no default, so adding them rewrites
-- nothing.

ALTER TABLE source_items ADD COLUMN received_at timestamptz;
UPDATE source_items SET received_at = created_at WHERE received_at IS NULL;
ALTER TABLE source_items ALTER COLUMN received_at SET DEFAULT now();
ALTER TABLE source_items ALTER COLUMN received_at SET NOT NULL;

ALTER TABLE source_items DROP CONSTRAINT source_items_state_check;
ALTER TABLE source_items ADD CONSTRAINT source_items_state_check
    CHECK (state IN ('pending', 'integrated', 'failed', 'dismissed'));

ALTER TABLE news_item_sources
    ADD COLUMN relation        text,
    ADD COLUMN reason          text,
    ADD COLUMN candidate_id    text,
    ADD COLUMN model           text,
    ADD COLUMN basis           text,
    ADD COLUMN decided_title   text,
    ADD COLUMN decided_summary text,
    ADD COLUMN decided_at      timestamptz;
