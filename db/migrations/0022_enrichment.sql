-- Agentic enrichment: what a run decided, what it produced, and the browser it left behind.
--
-- The second half of motet#102 — 0018 was the first, the connectors that say *which* sites
-- may be fetched from and with whose credentials. Decided by Tadas on 2026-09-13 in that
-- issue's design session (options A2, B3, C2, D2, F2, G3, H1); AGENTS.md, "The article
-- behind the newsletter", is the record.
--
-- Three things here, and the shape of each follows from one of the picks.
--
--   * **`source_items` learns what happened to it** (H1: the article goes over `text` and
--     the preview moves to `original_text`). Nine columns on a table used exactly the way
--     it is already used, not a second table joined per row: every one of them is a fact
--     about one item, and the lifecycle view reads them beside `last_error` and
--     `label_error`, which are already there.
--   * **`enrich_runs` is a table used as a log** — one row per run, appended, never
--     updated. It holds the redacted transcript, which is the only record of *how* a run
--     reached its answer; the model writes a different one every time, so there is nothing
--     to recompute it from.
--   * **`browser_states` is the vault's third kind of sealed record**, after
--     `source_credentials` and `connectors`: a Playwright storage state — cookies and
--     localStorage — per user per domain, which is what makes "log in once per domain"
--     true. Invariant 8 applies to it exactly as to the other two: envelope columns, a
--     worker-only decrypt, and an AAD that binds the ciphertext to the row it is on.

-- --------------------------------------------------------------------------------------
-- What the newsletter linked to.
-- --------------------------------------------------------------------------------------
--
-- The deterministic rule the design picked (A2) is "does this item link to a site the owner
-- has added" — and the answer was not in the database, because `motet_sources.extract`
-- deliberately throws every href away: a briefing is spoken, and a 200-character tracking
-- redirect is not a sentence. So the links are kept here, beside the text they were cut out
-- of, and `text` is unchanged.
--
-- **Rows written before this migration have an empty array and can never be enriched.**
-- The raw message is not retained (the deferred half of motet#91), so there is nothing to
-- backfill from. A re-poll inside the window is the repair, and after the window there is
-- none; that is the honest cost of not keeping raw bytes.
ALTER TABLE source_items ADD COLUMN links text[] NOT NULL DEFAULT '{}';

-- --------------------------------------------------------------------------------------
-- What enrichment did with it.
-- --------------------------------------------------------------------------------------
--
-- `enrich_status` is NULL for every item nothing was ever decided about — a paste with no
-- matching site, an item ingested before this shipped — and that is a third state rather
-- than a default: "not applicable" and "queued and waiting" must not be the same value,
-- because one of them means a worker owes the item something.
ALTER TABLE source_items ADD COLUMN enrich_status text
    CHECK (enrich_status IS NULL
           OR enrich_status IN ('queued', 'running', 'done', 'failed', 'skipped'));
-- Which link the run was pointed at. Recorded when the job is queued, so a run that never
-- started still says what it would have fetched.
ALTER TABLE source_items ADD COLUMN article_url text;
-- The site connector's domain, which is what keys the browser state: a click-tracking host
-- (`url3396.example.com`) collapses to the site (`example.com`), so one login serves every
-- link a publisher sends.
ALTER TABLE source_items ADD COLUMN enrich_domain text;
ALTER TABLE source_items ADD COLUMN enrich_error text;
ALTER TABLE source_items ADD COLUMN enriched_at timestamptz;
-- The newsletter's own body, kept when the article replaces it. Not a copy for safety's
-- sake: the lifecycle view's stage 1 reports what *arrived*, and after enrichment `text` is
-- no longer that. NULL means `text` is still the original.
ALTER TABLE source_items ADD COLUMN original_text text;

-- Answering "is this item enriching" for one user's held list and ingestion panel. Partial,
-- because the overwhelming majority of rows have no enrichment state at all.
CREATE INDEX source_items_enrich_idx ON source_items (user_id, enrich_status)
    WHERE enrich_status IS NOT NULL;

-- --------------------------------------------------------------------------------------
-- One row per run.
-- --------------------------------------------------------------------------------------
--
-- **The transcript stored here has already been redacted**, by `motet_enrich.redact`, on
-- the service side — before it crossed the network, let alone reached this table. The rule
-- that does the work is not a pattern: a tool result from anything other than the browser
-- server is *replaced* by a note giving its size, because the body of a sign-in email is
-- precisely what must not be written down. See that module for what the patterns add and
-- what they cannot.
--
-- No foreign key to `connectors`: a run outlives the site row it used, and "which site was
-- this" is answered by `domain` here. The source item *is* a foreign key, because a run
-- with no item is meaningless and a deleted item should take its runs with it.
CREATE TABLE enrich_runs (
    id              text        PRIMARY KEY,
    source_item_id  text        NOT NULL REFERENCES source_items (id) ON DELETE CASCADE,
    user_id         text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    domain          text        NOT NULL,
    status          text        NOT NULL
                                CHECK (status IN ('ok', 'blocked', 'capped', 'timeout',
                                                  'failed', 'skipped')),
    -- What it cost and how hard it worked. `cost_usd` is the agent's own completions as the
    -- service reported them; it is what the rolling per-user daily cap is summed from, which
    -- is why it is NOT NULL with a default rather than nullable — a run that reported no
    -- cost must count as zero in that sum rather than vanish from it.
    tool_calls      integer     NOT NULL DEFAULT 0,
    cost_usd        numeric(10, 6) NOT NULL DEFAULT 0,
    article_chars   integer     NOT NULL DEFAULT 0,
    login_performed boolean     NOT NULL DEFAULT false,
    transcript      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    error           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

-- The daily cap's query: this user's runs since a moment. `INCLUDE (cost_usd)` so the sum
-- *can* be answered without visiting the table; on a table this write-heavy the visibility
-- map often will not be current enough for an index-only scan, so read it as a cheap
-- improvement rather than a guarantee.
CREATE INDEX enrich_runs_user_time_idx ON enrich_runs (user_id, started_at DESC)
    INCLUDE (cost_usd);
-- The lifecycle view's "the newest run for this item". Ordered by time and not by the id,
-- because the id is random hex rather than a sequence: `ORDER BY id DESC` would pick an
-- arbitrary run and be right about half the time.
CREATE INDEX enrich_runs_item_idx ON enrich_runs (source_item_id, started_at DESC, id DESC);

-- --------------------------------------------------------------------------------------
-- The browser the last run left behind.
-- --------------------------------------------------------------------------------------
--
-- One row per (user, domain), replaced wholesale by each run that produces one. There is no
-- history: a storage state is a live session, and yesterday's is a session that has been
-- superseded rather than a version of anything.
--
-- The envelope is 0003's and 0018's, under the AAD `user_id:<domain>:browser_state` — the
-- same three-slot shape, so a ciphertext moved onto another user's row or another domain's
-- fails to authenticate rather than logging one account into another's site. Unlike the
-- other two tables the envelope columns are NOT NULL as a group: a row exists only because
-- a run sealed something into it, so there is no "row without a secret" state to model.
CREATE TABLE browser_states (
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    domain      text        NOT NULL,
    ciphertext  bytea       NOT NULL CHECK (length(ciphertext) > 0),
    nonce       bytea       NOT NULL CHECK (length(nonce) = 12),
    wrapped_dek bytea       NOT NULL CHECK (length(wrapped_dek) > 0),
    backend     text        NOT NULL,
    key_name    text        NOT NULL,
    -- How many cookies it holds. Plaintext on purpose and the only thing about the state
    -- that is: "the session was saved and it is empty" and "no session was saved" are
    -- otherwise the same row, and an agent debugging a login loop cannot open the rest.
    cookies     integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, domain)
);

-- --------------------------------------------------------------------------------------
-- The claim's two readers learn about the new queue.
-- --------------------------------------------------------------------------------------
--
-- `repo._HELD_WHERE` and `repo.INGESTION_SQL` both ask "does this source item have a job",
-- and both asked it of the `integrate` queue alone, because until now that was the only
-- queue an item could be waiting on. An item waiting on `enrich` would have read as *held*
-- — offered on the panel, claimable a second time — and would have appeared on neither
-- surface once claimed, breaking the property those two queries exist to hold: every item
-- is on exactly one of them.
--
-- Both now ask about `queue IN ('integrate', 'enrich')`. 0005's index is partial on
-- `queue = 'integrate'` and therefore no longer covers the predicate on its own, so this is
-- its twin: with both present the planner answers the OR from a bitmap of the two rather
-- than from a sequential scan of every job ever run, which is motet#49's failure.
CREATE INDEX jobs_enrich_source_item_idx ON jobs ((payload ->> 'source_item_id'))
    WHERE queue = 'enrich';
