-- PROTOTYPE (proto/local-ux): connectors — the credentials an *agentic* ingestion step
-- uses to fetch the full article behind a newsletter preview.
--
-- Two kinds, and the kind is what decides how the credential is used downstream:
--
--   * `site` — a username and (optionally) a password for one domain, e.g.
--     theinformation.com. **This is the trigger for a browser agent**: a source item whose
--     links resolve to that domain gets an agentic session (Playwright) that logs in with
--     these and reads the article. The password may be absent — The Information is a
--     passwordless email-code login, so username-only has to be a legal row.
--   * `mcp` — a remote MCP server reached over OAuth 2.1 (the MCP spec's own auth). The
--     secret is the token set the authorization produced; the server is handed to the
--     agent session as a tool source, optionally restricted to a list of domains.
--
-- **There is no plaintext secret column, exactly as `source_credentials` has none**
-- (invariant 8). The envelope shape is copied from 0003 — ciphertext, nonce, wrapped DEK,
-- plus backend/key provenance — with the AAD `user_id:connector_id:kind`. The columns are
-- nullable *as a group*: a `site` row with no password stores nothing, and the check below
-- refuses a half-written envelope. `username` is plaintext deliberately; it is an
-- identifier the UI has to render back, not a secret.
--
-- Invariant 12: a new role for the vault (a second kind of sealed record) and the
-- enrichment stage this feeds are both design-session items; this table exists so the
-- prototype can be exercised end to end, and the session decides what survives.
CREATE TABLE connectors (
    id                     text        PRIMARY KEY,
    user_id                text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind                   text        NOT NULL CHECK (kind IN ('site', 'mcp')),
    label                  text        NOT NULL,
    -- `site`: the one domain the credential is for, lowercase, no scheme, no path.
    domain                 text,
    -- `mcp`: the domains this server is worth handing to an agent for. Empty means "any".
    domains                text[]      NOT NULL DEFAULT '{}',
    -- `mcp`: the server URL as the user pasted it, query string and all — a `?servers=`
    -- selection hint is part of what they meant.
    url                    text,
    -- `site`: the login identifier. Not a secret.
    username               text,
    -- The sealed secret: a password (`site`) or a JSON token set (`mcp`). All-or-nothing.
    ciphertext             bytea       CHECK (ciphertext IS NULL OR length(ciphertext) > 0),
    nonce                  bytea       CHECK (nonce IS NULL OR length(nonce) = 12),
    wrapped_dek            bytea       CHECK (wrapped_dek IS NULL OR length(wrapped_dek) > 0),
    backend                text,
    key_name               text,
    -- `mcp`: when the sealed access token stops working. The worker refreshes past it.
    secret_expires_at      timestamptz,
    -- `mcp`: what OAuth discovery and dynamic client registration produced. None of it is
    -- secret — strad's client_id is a signed public record, and the endpoints are on a
    -- `.well-known` document — and the worker needs the token endpoint and client id to
    -- refresh without re-running discovery.
    oauth_issuer           text,
    oauth_client_id        text,
    oauth_token_endpoint   text,
    oauth_resource         text,
    -- 'ready' can be used; 'needs_auth' is an `mcp` row that has not been authorized (or
    -- whose grant died); 'error' is a `last_error` worth reading.
    status                 text        NOT NULL CHECK (status IN ('ready', 'needs_auth', 'error')),
    last_error             text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT connectors_kind_shape CHECK (
        (kind = 'site' AND domain IS NOT NULL AND username IS NOT NULL AND url IS NULL)
        OR (kind = 'mcp' AND url IS NOT NULL AND domain IS NULL AND username IS NULL)
    ),
    CONSTRAINT connectors_envelope_whole CHECK (
        (ciphertext IS NULL AND nonce IS NULL AND wrapped_dek IS NULL
            AND backend IS NULL AND key_name IS NULL)
        OR (ciphertext IS NOT NULL AND nonce IS NOT NULL AND wrapped_dek IS NOT NULL
            AND backend IS NOT NULL AND key_name IS NOT NULL)
    )
);

CREATE INDEX connectors_user_idx ON connectors (user_id, kind, created_at, id);

-- One `site` credential per domain per user: the agent asks "what logs me into this
-- domain" and must get one answer.
CREATE UNIQUE INDEX connectors_site_domain_idx
    ON connectors (user_id, domain) WHERE kind = 'site';

-- The MCP authorization rides the existing `oauth_states` table, the way sign-in did in
-- 0004: a third provider value and a column naming the connector, rather than a second
-- table with its own single-use consume path and expiry sweep. Dropped by name, without
-- IF EXISTS, for 0004's reason — a wrong assumption about the constraint's name should
-- fail here rather than leave two constraints disagreeing.
ALTER TABLE oauth_states DROP CONSTRAINT oauth_states_provider_check;
ALTER TABLE oauth_states ADD CONSTRAINT oauth_states_provider_check
    CHECK (provider IN ('gmail', 'google', 'mcp'));
ALTER TABLE oauth_states ADD COLUMN connector_id text
    REFERENCES connectors (id) ON DELETE CASCADE;
