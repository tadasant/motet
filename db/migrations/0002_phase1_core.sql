-- Phase 1's data model: Source -> Source Item -> News Item -> Episode -> Segment -> Claim.
--
-- Three shapes here are invariants rather than convenience, and each is enforced by a
-- constraint so that a bug in application code fails at the write rather than at the
-- listen:
--
--   * A source item integrates into exactly ONE news item (`news_item_sources` is unique
--     on `source_item_id`). Dedup that double-counted would speak a story twice.
--   * Every claim carries a span into a real source item, and the span is non-empty
--     (invariant 3). A claim row cannot exist without one.
--   * Read state hangs off the NEWS ITEM (invariant 5) — not off an episode, not off a
--     source item. That is what makes "listened past it" and "marked read on the web" the
--     same fact.
--
-- Single hardcoded account (Phase 1): the owner row is inserted here rather than
-- configured, because "one user, no signup" is the design, not a default.

-- The pipeline serializes some work by key: ingestion is per user (invariant 6), because
-- dedup compares a new source item against the current window and two concurrent runs
-- would race into duplicate news items. Workers take a Postgres advisory lock on this
-- key; a job whose key is busy is deferred rather than run.
ALTER TABLE jobs ADD COLUMN serialize_key text;

CREATE TABLE users (
    id         text        PRIMARY KEY,
    email      text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The single Phase 1 account. Signup, OAuth, and multi-tenancy are Phase 3.
INSERT INTO users (id, email) VALUES ('motet-owner', NULL);

CREATE TABLE sources (
    id         text        PRIMARY KEY,
    user_id    text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind       text        NOT NULL CHECK (kind IN ('paste')),
    name       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sources_user_idx ON sources (user_id);

-- Paste-in is Phase 1's only ingestion route. Gmail and X arrive in Phase 2 as further
-- `kind` values, which is why the column exists with one legal value today.
INSERT INTO sources (id, user_id, kind, name) VALUES ('src_paste', 'motet-owner', 'paste', 'Pasted text');

CREATE TABLE source_items (
    id            text        PRIMARY KEY,
    user_id       text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source_id     text        NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    title         text        NOT NULL,
    text          text        NOT NULL,
    state         text        NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending', 'integrated', 'failed')),
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    integrated_at timestamptz
);

CREATE INDEX source_items_user_idx ON source_items (user_id, created_at, id);

CREATE TABLE news_items (
    id         text        PRIMARY KEY,
    user_id    text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      text        NOT NULL,
    summary    text        NOT NULL,
    read_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The two queries that matter: the whole backlog, and the unread window dedup integrates
-- against / episodes are assembled from.
CREATE INDEX news_items_user_idx ON news_items (user_id, created_at, id);
CREATE INDEX news_items_unread_idx ON news_items (user_id, created_at, id) WHERE read_at IS NULL;

CREATE TABLE news_item_sources (
    news_item_id   text    NOT NULL REFERENCES news_items (id) ON DELETE CASCADE,
    -- UNIQUE, not just part of the primary key: one source item belongs to exactly one
    -- news item. An integrator that reported a merge and a create for the same item would
    -- otherwise produce a story that gets spoken twice.
    source_item_id text    NOT NULL UNIQUE REFERENCES source_items (id) ON DELETE CASCADE,
    position       integer NOT NULL,
    PRIMARY KEY (news_item_id, source_item_id)
);

CREATE TABLE episodes (
    id               text        PRIMARY KEY,
    user_id          text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title            text        NOT NULL,
    -- pending -> scripting -> rendering -> ready, or failed. One state per pipeline stage
    -- that can be retried independently.
    state            text        NOT NULL DEFAULT 'pending'
                                 CHECK (state IN ('pending', 'scripting', 'rendering', 'ready', 'failed')),
    max_duration_ms  integer     NOT NULL CHECK (max_duration_ms > 0),
    duration_ms      integer     NOT NULL DEFAULT 0,
    audio_key        text,
    audio_bytes      bigint,
    audio_media_type text,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz
);

-- The feed query: a user's published episodes, newest first.
CREATE INDEX episodes_feed_idx ON episodes (user_id, published_at DESC) WHERE state = 'ready';

CREATE TABLE episode_segments (
    id           text        PRIMARY KEY,
    episode_id   text        NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    news_item_id text        NOT NULL REFERENCES news_items (id) ON DELETE CASCADE,
    position     integer     NOT NULL,
    text         text        NOT NULL DEFAULT '',
    -- Where this segment starts in the episode's audio. We own playback position
    -- (invariant 4) — it is never read back out of a player or a vendor SDK — and this is
    -- the anchor Phase 2's `spoken_through_ms` resolves against.
    start_ms     integer     NOT NULL DEFAULT 0,
    duration_ms  integer     NOT NULL DEFAULT 0,
    UNIQUE (episode_id, position),
    UNIQUE (episode_id, news_item_id)
);

CREATE TABLE segment_claims (
    id             text    PRIMARY KEY,
    segment_id     text    NOT NULL REFERENCES episode_segments (id) ON DELETE CASCADE,
    position       integer NOT NULL,
    -- What gets spoken. `span_start`/`span_end` point at the verbatim evidence in
    -- `source_item_id`, which may be a paraphrase of this text rather than equal to it.
    text           text    NOT NULL,
    source_item_id text    NOT NULL REFERENCES source_items (id) ON DELETE CASCADE,
    span_start     integer NOT NULL,
    span_end       integer NOT NULL,
    UNIQUE (segment_id, position),
    -- Invariant 3 at the storage layer: a claim without a real, non-empty span cannot be
    -- written at all, so it can never reach TTS.
    CONSTRAINT segment_claims_span_is_real CHECK (span_start >= 0 AND span_end > span_start)
);

CREATE INDEX segment_claims_segment_idx ON segment_claims (segment_id, position);

-- The credential on the private RSS feed. It is a bearer secret in a URL, deliberately:
-- podcast clients handle a secret in the URL far better than they handle HTTP auth, and
-- the feed is useless without one. Stored in the clear rather than hashed because the
-- user has to be able to READ it back — a feed URL is copied to a new device months
-- later, and an unrecoverable token would force a rotation that breaks every existing
-- subscription. Rotation is available for the case where it leaks.
CREATE TABLE feed_tokens (
    token      text        PRIMARY KEY,
    user_id    text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE INDEX feed_tokens_active_idx ON feed_tokens (user_id, created_at DESC) WHERE revoked_at IS NULL;
