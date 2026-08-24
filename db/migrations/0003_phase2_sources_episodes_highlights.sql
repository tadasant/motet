-- Phase 2's data model: connected sources, smart episodes, highlights, and the timing a
-- transcript needs to become subtitles.
--
-- Four shapes here are load-bearing, and each is a constraint rather than a convention so
-- that a bug in application code fails at the write rather than at the listen:
--
--   * A source credential has **no plaintext column to put a token in** (invariant 8).
--     The row holds a ciphertext, a nonce, and a wrapped DEK; the key that would open it
--     lives in Cloud KMS. See `motet_vault.envelope`.
--   * A polled message integrates **at most once**: `source_items` is unique on
--     `(source_id, external_id)`, so re-polling a Gmail mailbox after a crash cannot
--     produce a second copy of the same newsletter.
--   * A highlight anchors to a **source span**, not to a claim row and not to an audio
--     offset. Claims are deleted and rewritten on every script retry and offsets move on
--     every re-render; the source item's text never changes. See `highlights` below.
--   * Read state still hangs off the NEWS ITEM and nowhere else (invariant 5).
--     `episodes.listened_through_ms` is a playback *position*, and the only thing it is
--     allowed to do is decide which news items to mark read.

-- --- connected sources -------------------------------------------------------------

-- Gmail joins paste-in. `kind` was written with one legal value in 0002 precisely so that
-- this would be a widening rather than a redesign.
ALTER TABLE sources DROP CONSTRAINT sources_kind_check;
ALTER TABLE sources ADD CONSTRAINT sources_kind_check CHECK (kind IN ('paste', 'gmail'));

-- Per-source settings the user chose: which Gmail query to poll, which label. Opaque to
-- the schema because it is adapter-shaped, and a column per provider setting would mean a
-- migration every time a provider grows an option.
ALTER TABLE sources ADD COLUMN config jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Where the last poll got to — a Gmail history id, or a date watermark on first sync.
-- Distinct from `config` because one is the user's intent and the other is our bookmark,
-- and conflating them means "change your Gmail query" silently re-ingests the archive.
ALTER TABLE sources ADD COLUMN sync_state jsonb NOT NULL DEFAULT '{}'::jsonb;

-- A source that is connected but paused should stop being polled without being deleted:
-- deleting it would orphan every source item it produced.
ALTER TABLE sources ADD COLUMN active boolean NOT NULL DEFAULT true;
ALTER TABLE sources ADD COLUMN last_polled_at timestamptz;
ALTER TABLE sources ADD COLUMN last_error text;

-- The credential vault. **There is deliberately no plaintext column here.**
--
-- Envelope encryption, per invariant 8: `ciphertext` is the secret under a per-record DEK,
-- `wrapped_dek` is that DEK under a Cloud KMS KEK, and the AAD binding both is
-- `user_id:source_id:provider` — derived at use, never stored, so a row cannot lie about
-- what it belongs to. Moving a ciphertext between rows produces an authentication failure
-- instead of one account holding another's mailbox.
--
-- `backend` and `key_name` are provenance for a future re-key, not instructions: unsealing
-- uses the key manager the *process* is configured with, because a row that could name its
-- own decryptor could name a weaker one.
CREATE TABLE source_credentials (
    id           text        PRIMARY KEY,
    user_id      text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source_id    text        NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    -- Which third party the credential is for. Part of the AAD, so it is not cosmetic.
    provider     text        NOT NULL CHECK (provider IN ('gmail')),
    -- 'refresh' is the long-lived grant; 'access' is the short-lived token derived from
    -- it. Both are sealed; neither is ever written in the clear.
    purpose      text        NOT NULL CHECK (purpose IN ('refresh', 'access')),
    ciphertext   bytea       NOT NULL CHECK (length(ciphertext) > 0),
    nonce        bytea       NOT NULL CHECK (length(nonce) = 12),
    wrapped_dek  bytea       NOT NULL CHECK (length(wrapped_dek) > 0),
    backend      text        NOT NULL,
    key_name     text        NOT NULL,
    -- Space-separated OAuth scopes this grant actually carries. Recorded so that
    -- incremental consent can tell "the user has not granted this yet" from "the token
    -- expired", which are different repairs.
    scopes       text        NOT NULL DEFAULT '',
    expires_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    -- One credential per purpose per source: re-running consent replaces rather than
    -- accumulates, so there is never an ambiguity about which token is current.
    UNIQUE (source_id, purpose)
);

CREATE INDEX source_credentials_user_idx ON source_credentials (user_id, provider);

-- An in-flight OAuth authorization. The `state` parameter is the CSRF defence, and it has
-- to be *stored* to be checked — a state a callback could validate on its own would defend
-- against nothing. PKCE verifier alongside it, because Google's installed-app guidance
-- applies to any client whose redirect a third party could reach.
--
-- Rows are short-lived and swept on use; `expires_at` bounds the ones that never come
-- back, which is the normal outcome when a user closes the consent tab.
CREATE TABLE oauth_states (
    state         text        PRIMARY KEY,
    user_id       text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider      text        NOT NULL CHECK (provider IN ('gmail')),
    source_id     text        REFERENCES sources (id) ON DELETE CASCADE,
    code_verifier text        NOT NULL,
    redirect_uri  text        NOT NULL,
    scopes        text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);

CREATE INDEX oauth_states_expiry_idx ON oauth_states (expires_at);

-- --- polled source items -----------------------------------------------------------

-- The provider's own id for the thing this came from — a Gmail message id. Nullable
-- because pasted text has no external identity.
ALTER TABLE source_items ADD COLUMN external_id text;

-- Idempotent ingestion. A poll that crashed after fetching but before committing its
-- cursor re-fetches the same messages; without this each retry would add another copy of
-- the newsletter, and dedup would then have to unpick it downstream.
CREATE UNIQUE INDEX source_items_external_idx
    ON source_items (source_id, external_id) WHERE external_id IS NOT NULL;

-- --- smart episodes ----------------------------------------------------------------

-- 'manual' is Phase 1's "everything unread, oldest first, until the cap". 'smart' selects
-- by a rule. The column defaults to 'manual' so every existing episode keeps meaning what
-- it meant.
ALTER TABLE episodes ADD COLUMN kind text NOT NULL DEFAULT 'manual'
    CHECK (kind IN ('manual', 'smart'));

-- The rule this episode was built from, as a snapshot rather than a reference.
--
-- A snapshot on purpose: an episode is a historical artifact, and "why does this episode
-- contain these stories" must stay answerable after the rule that produced it is edited.
-- A foreign key to a mutable rule row would make the answer change under the listener.
ALTER TABLE episodes ADD COLUMN rule jsonb;
ALTER TABLE episodes ADD CONSTRAINT episodes_smart_has_rule
    CHECK (kind <> 'smart' OR rule IS NOT NULL);

-- --- read state, from the audio side -----------------------------------------------

-- How far into this episode the listener has actually got.
--
-- Invariant 4: *we* own playback position. This is written by our own API from a client's
-- report and is never read back out of a vendor SDK. Invariant 5 is what it is *for*: the
-- only thing this column may do is decide which news items are marked read, so listening
-- past a story on a walk and ticking it off on the backlog screen remain one fact.
--
-- Monotonic in the repository layer, not here: a client that seeks backwards is reviewing,
-- not un-listening, and a position that could go down would un-mark stories as a side
-- effect of scrubbing.
ALTER TABLE episodes ADD COLUMN listened_through_ms integer NOT NULL DEFAULT 0
    CHECK (listened_through_ms >= 0);

-- --- subtitles ---------------------------------------------------------------------

-- Where each claim sits inside the episode's audio.
--
-- The transcript already pairs every spoken sentence with the source span that evidences
-- it (invariant 3). Adding timing to the claim turns that same structure into subtitles
-- and chapters with nothing new to maintain — which is the point of having built the
-- script contract this way.
--
-- Filled by the TTS stage from the *measured* segment duration, apportioned across the
-- segment's claims by character count. See `motet_workers.handlers` for why that is an
-- apportionment rather than a per-claim synthesis.
ALTER TABLE segment_claims ADD COLUMN start_ms integer NOT NULL DEFAULT 0
    CHECK (start_ms >= 0);
ALTER TABLE segment_claims ADD COLUMN duration_ms integer NOT NULL DEFAULT 0
    CHECK (duration_ms >= 0);

-- --- highlights --------------------------------------------------------------------

-- "Save that bit" — the `save_highlight` platform tool, and the visual equivalent.
--
-- **Anchoring is the open question this table answers, and the answer is: anchor to the
-- source span.** The three candidates and why the others lose:
--
--   * *A claim id.* Rejected. The script stage rewrites an episode's claims wholesale on
--     every retry (`replace_segments` deletes and re-inserts), so a claim id is not stable
--     across a re-render. A highlight would silently detach from a story that got rescripted.
--   * *An audio offset.* Rejected. Offsets move whenever the audio is re-synthesized, and
--     they say nothing at all on the visual surface, where there is no audio to offset into.
--   * *A source span.* Chosen. `source_items.text` is the raw ingested document and is
--     never rewritten — it is the one immutable thing in the pipeline, and it is already
--     the anchor every claim uses (invariant 3). A highlight anchored there survives
--     re-scripting, re-rendering, and dedup merges, and it means the same thing whether it
--     was saved by voice or by tapping the transcript.
--
-- `quote` is denormalized alongside the span deliberately: it is what the user actually
-- saved, and a highlight that renders as an empty string because its source item was
-- deleted is worse than one that outlives its source.
--
-- `episode_id` and `anchor_ms` are **provenance, not the anchor** — they record where the
-- user was when they saved it, so "take me back to that moment" works while the audio
-- exists, and nothing breaks when it does not.
CREATE TABLE highlights (
    id             text        PRIMARY KEY,
    user_id        text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    news_item_id   text        NOT NULL REFERENCES news_items (id) ON DELETE CASCADE,
    source_item_id text        NOT NULL REFERENCES source_items (id) ON DELETE CASCADE,
    span_start     integer     NOT NULL,
    span_end       integer     NOT NULL,
    quote          text        NOT NULL,
    note           text,
    -- Provenance. Nulled rather than cascaded on episode deletion: losing the episode
    -- must not lose the highlight.
    episode_id     text        REFERENCES episodes (id) ON DELETE SET NULL,
    anchor_ms      integer,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- The same rule `segment_claims` carries: an anchor that resolves to nothing is not an
    -- anchor, so it cannot be written at all.
    CONSTRAINT highlights_span_is_real CHECK (span_start >= 0 AND span_end > span_start),
    -- Saving the same passage twice is a no-op rather than a second row. Voice and touch
    -- can both reach for the same sentence, and two highlights of one sentence is a bug
    -- the user would have to clean up by hand.
    UNIQUE (user_id, source_item_id, span_start, span_end)
);

CREATE INDEX highlights_user_idx ON highlights (user_id, created_at DESC, id);
CREATE INDEX highlights_news_item_idx ON highlights (news_item_id);
