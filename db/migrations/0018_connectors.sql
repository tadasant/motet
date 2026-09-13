-- Connectors: the sites and remote MCP servers agentic enrichment may use (motet#102).
--
-- Decided by Tadas on 2026-09-13, in motet#102's design session (options B3 and E1; the
-- issue is the evidence, the session is recorded in AGENTS.md under "Credentials are a
-- second kind of sealed record"). Two kinds, and the kind decides how a row is used:
--
--   * `site` — one domain the owner has *added*. **Adding a site is the opt-in**: an
--     article is only ever fetched from a domain with a `site` row, so this table is the
--     allowlist as well as the credential store. The username and the password are both
--     optional — a site readable from the newsletter's own link needs neither, and a site
--     that logs in by emailed code or magic link needs only the address.
--   * `mcp` — a remote MCP server reached over OAuth 2.1 (the MCP specification's own
--     authorization). The secret is the token set consent produced. Kept (option E1) with
--     its risk stated at the moment of connecting: the agent that is handed this server
--     also reads untrusted web pages, so a hostile page can steer it into using the
--     server's tools with the owner's account. `risk_acknowledged_at` is when the owner
--     said they understood that, and the API refuses to create an `mcp` row without it.
--
-- **There is no plaintext secret column, exactly as `source_credentials` has none**
-- (invariant 8). The envelope is 0003's — ciphertext, nonce, wrapped DEK, plus backend
-- and key provenance — under the AAD `user_id:connector_id:kind`. The envelope columns are
-- nullable *as a group*, and a check refuses a half-written one. `username` is plaintext
-- deliberately: it is an identifier the screen renders back, not a secret.
CREATE TABLE connectors (
    id                     text        PRIMARY KEY,
    user_id                text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind                   text        NOT NULL CHECK (kind IN ('site', 'mcp')),
    label                  text        NOT NULL,
    -- `site`: the one domain, lowercase, no scheme, no path, no leading `www.`.
    domain                 text,
    -- `mcp`: the sites this server may be handed to the agent for. Empty means every site
    -- the owner has added — never an arbitrary one, because nothing is fetched elsewhere.
    domains                text[]      NOT NULL DEFAULT '{}',
    -- `mcp`: the server URL as the owner pasted it, query string and all — a selection
    -- hint in the query is part of what they meant.
    url                    text,
    -- `site`: the login identifier, when the site needs one.
    username               text,
    -- The sealed secret: a password (`site`) or a JSON token set (`mcp`). All-or-nothing.
    ciphertext             bytea       CHECK (ciphertext IS NULL OR length(ciphertext) > 0),
    nonce                  bytea       CHECK (nonce IS NULL OR length(nonce) = 12),
    wrapped_dek            bytea       CHECK (wrapped_dek IS NULL OR length(wrapped_dek) > 0),
    backend                text,
    key_name               text,
    -- `mcp`: when the sealed access token stops working. A worker refreshes past it.
    secret_expires_at      timestamptz,
    -- `mcp`: what discovery and dynamic client registration produced. None of it is
    -- secret — a public client's id, and endpoints read off `.well-known` documents — and
    -- a worker needs the token endpoint, client id and resource to refresh without
    -- running discovery again.
    oauth_issuer           text,
    oauth_client_id        text,
    oauth_token_endpoint   text,
    oauth_resource         text,
    risk_acknowledged_at   timestamptz,
    -- 'ready' can be used; 'needs_auth' is an `mcp` row with no working grant yet (or one
    -- whose grant died); 'error' is a `last_error` worth reading.
    status                 text        NOT NULL CHECK (status IN ('ready', 'needs_auth', 'error')),
    last_error             text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT connectors_kind_shape CHECK (
        (kind = 'site' AND domain IS NOT NULL AND url IS NULL AND risk_acknowledged_at IS NULL)
        OR (kind = 'mcp' AND url IS NOT NULL AND domain IS NULL AND username IS NULL
            AND risk_acknowledged_at IS NOT NULL)
    ),
    -- A password with nobody to log in as is not a credential anyone can use.
    CONSTRAINT connectors_site_password_needs_username CHECK (
        kind <> 'site' OR ciphertext IS NULL OR username IS NOT NULL
    ),
    CONSTRAINT connectors_envelope_whole CHECK (
        (ciphertext IS NULL AND nonce IS NULL AND wrapped_dek IS NULL
            AND backend IS NULL AND key_name IS NULL)
        OR (ciphertext IS NOT NULL AND nonce IS NOT NULL AND wrapped_dek IS NOT NULL
            AND backend IS NOT NULL AND key_name IS NOT NULL)
    )
);

CREATE INDEX connectors_user_idx ON connectors (user_id, kind, created_at, id);

-- One `site` row per domain per user: "may I fetch from this domain, and as whom" must
-- have one answer.
CREATE UNIQUE INDEX connectors_site_domain_idx
    ON connectors (user_id, domain) WHERE kind = 'site';

-- An MCP authorization rides the existing `oauth_states` table, the way sign-in did in
-- 0004: a third provider value and a column naming the connector, rather than a second
-- table with its own single-use consume and expiry sweep. A column of its own rather than
-- a reuse of `source_id`, which is a foreign key to `sources`. The constraint is dropped by
-- name without IF EXISTS, for 0004's reason: a wrong assumption about its name should fail
-- here rather than leave two constraints disagreeing.
ALTER TABLE oauth_states DROP CONSTRAINT oauth_states_provider_check;
ALTER TABLE oauth_states ADD CONSTRAINT oauth_states_provider_check
    CHECK (provider IN ('gmail', 'google', 'mcp'));
ALTER TABLE oauth_states ADD COLUMN connector_id text
    REFERENCES connectors (id) ON DELETE CASCADE;
