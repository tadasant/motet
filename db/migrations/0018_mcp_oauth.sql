-- Motet as an OAuth authorization server for its own MCP endpoint (motet#111).
--
-- Tadas picked per-user auth for `/mcp` ("C2") on 2026-09-13. An MCP client discovers
-- Motet's authorization server, registers itself (RFC 7591), and sends its user through a
-- Google sign-in; what it gets back is an ordinary `auth_sessions` row, so `/mcp` and `/v1`
-- check it with the one function that already decides who may call this API. Nothing
-- here is a user system: every row still belongs to the one account, and the allowlist
-- that decides who may sign in decides who may authorize a client.

-- --- the pending authorization ---------------------------------------------------------

-- An MCP authorization rides a Google sign-in, so its in-flight state is an `oauth_states`
-- row like any other sign-in's: the same single-use consume, the same expiry sweep. What
-- it adds is the MCP client's own request — its client id, redirect URI, PKCE challenge
-- and state — which has to survive the round trip through Google to be honoured after it.
-- NULL on every other flow.
ALTER TABLE oauth_states ADD COLUMN mcp_request jsonb;

-- --- registered clients ------------------------------------------------------------------

-- One row per dynamic client registration. Registration is unauthenticated by design (it
-- is how a client that holds nothing starts), so a registration is only a record of a
-- client's redirect URIs and name: it grants nothing until a person on the allowlist signs
-- in and approves it. Rows nobody ever used are swept after a day.
CREATE TABLE mcp_oauth_clients (
    client_id   text        PRIMARY KEY,
    client_info jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX mcp_oauth_clients_created_idx ON mcp_oauth_clients (created_at);

-- --- authorization codes -----------------------------------------------------------------

-- Issued once a person approved the client, redeemed once at the token endpoint. Stored as
-- a hash, like a session token, because nothing needs the value after it is handed out.
CREATE TABLE mcp_oauth_codes (
    code_sha256                      text        PRIMARY KEY,
    client_id                        text        NOT NULL
        REFERENCES mcp_oauth_clients (client_id) ON DELETE CASCADE,
    user_id                          text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    email                            text        NOT NULL,
    scopes                           text[]      NOT NULL DEFAULT '{}',
    code_challenge                   text        NOT NULL,
    redirect_uri                     text        NOT NULL,
    redirect_uri_provided_explicitly boolean     NOT NULL,
    resource                         text,
    created_at                       timestamptz NOT NULL DEFAULT now(),
    expires_at                       timestamptz NOT NULL
);

CREATE INDEX mcp_oauth_codes_expiry_idx ON mcp_oauth_codes (expires_at);

-- --- refresh tokens ------------------------------------------------------------------------

-- The long-lived half of an MCP client's grant. The access token is a short `auth_sessions`
-- row; this is what mints the next one, and rotating it deletes the old pair. `session_id`
-- names the access token issued beside it so that revoking either revokes both. Not a
-- foreign key: expired sessions are swept on their own schedule.
CREATE TABLE mcp_oauth_refresh_tokens (
    token_sha256 text        PRIMARY KEY,
    client_id    text        NOT NULL REFERENCES mcp_oauth_clients (client_id) ON DELETE CASCADE,
    user_id      text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    email        text        NOT NULL,
    scopes       text[]      NOT NULL DEFAULT '{}',
    resource     text,
    session_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);

CREATE INDEX mcp_oauth_refresh_tokens_user_idx ON mcp_oauth_refresh_tokens (user_id);
CREATE INDEX mcp_oauth_refresh_tokens_expiry_idx ON mcp_oauth_refresh_tokens (expires_at);

-- --- which client a session was issued to --------------------------------------------------

-- NULL for a browser that signed in and for the staging mint. Set for an MCP access token,
-- so the token endpoint's revocation can check the token belongs to the client revoking it,
-- and so deleting a client's registration takes its live tokens with it.
ALTER TABLE auth_sessions
    ADD COLUMN mcp_client_id text REFERENCES mcp_oauth_clients (client_id) ON DELETE CASCADE;
