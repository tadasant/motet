-- PROTOTYPE (proto/local-ux): agentic enrichment — a source item that is only a preview of
-- an article gets the full article fetched by a browser agent before dedup sees it.
--
-- Three things, and each is a design-session item under invariant 12 (see
-- proto/issues/11-agentic-enrichment.md): a new pipeline stage (`triage`, one cheap model
-- call at the top of integrate), a new queue (`enrich`, a Pi coding-agent run driving a
-- headless browser and the user's MCP servers), and a second kind of sealed record in the
-- vault (a browser storage state per user per domain).
--
-- **The email text is never lost.** When the article replaces `text`, the preview moves to
-- `original_text`; a highlight or a claim that cited the preview would otherwise anchor
-- into a string that no longer exists. Nothing cites a held item yet, so this is a
-- retention rule rather than a migration of anchors.

ALTER TABLE source_items
    -- What triage decided and why. NULL until the integrate job has run triage once; the
    -- second integrate pass (after enrichment) skips it, so this is written at most once.
    ADD COLUMN triage_decision text CHECK (triage_decision IN ('raw', 'fetch')),
    ADD COLUMN triage_reason   text,
    -- The article the preview links to, as triage read it off the text. Only for `fetch`.
    ADD COLUMN article_url     text,
    -- The enrichment's own life: NULL when triage said `raw` or never ran; `skipped` when
    -- triage said `fetch` but nothing could be tried; `pending` while the enrich job is
    -- queued, `running` while the agent is on it, and `done`/`failed` afterwards. A failed
    -- enrichment still integrates — the preview is better than nothing.
    ADD COLUMN enrich_status   text CHECK (
        enrich_status IN ('pending', 'running', 'done', 'failed', 'skipped')
    ),
    ADD COLUMN enrich_error    text,
    -- The email's extracted text, kept when the article replaces `text`.
    ADD COLUMN original_text   text,
    ADD COLUMN enriched_at     timestamptz;

-- One row per agent run, for transcript review. The transcript is stored **redacted** —
-- the raw stream carries the mailbox search results, the login email's body with its magic
-- link, and whatever the page returned — and it is a list of compact entries rather than
-- Pi's own event stream, so the API can hand it to the SPA as-is.
CREATE TABLE enrich_runs (
    id              text        PRIMARY KEY,
    source_item_id  text        NOT NULL REFERENCES source_items (id) ON DELETE CASCADE,
    user_id         text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text        NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    tool_calls      integer     NOT NULL DEFAULT 0,
    -- What the agent's own usage accounting says the run cost, in USD. NULL when the run
    -- never reported (a timeout, a crash before the first turn).
    cost_usd        numeric(10, 4),
    transcript      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    article_chars   integer     NOT NULL DEFAULT 0,
    login_performed boolean     NOT NULL DEFAULT false,
    error           text
);

CREATE INDEX enrich_runs_source_item_idx ON enrich_runs (source_item_id, started_at DESC);

-- The browser's cookies and local storage for one site, sealed with the vault, so the
-- second article on a domain needs no login. Same envelope as `connectors` and
-- `source_credentials` (invariant 8); the AAD is `user_id:<domain>:browser_state`, so a
-- state moved between users or domains fails to authenticate. `cookies` is a count, for
-- the UI and the log line, never the cookies.
CREATE TABLE browser_states (
    user_id     text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    domain      text        NOT NULL,
    ciphertext  bytea       NOT NULL CHECK (length(ciphertext) > 0),
    nonce       bytea       NOT NULL CHECK (length(nonce) = 12),
    wrapped_dek bytea       NOT NULL CHECK (length(wrapped_dek) > 0),
    backend     text        NOT NULL,
    key_name    text        NOT NULL,
    cookies     integer     NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, domain)
);
