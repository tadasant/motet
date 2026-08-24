-- Google Sign-In for the SPA: a browser session that is not a hand-typed bearer token.
--
-- **This is not a user system.** Motet still has exactly one account — `motet-owner`,
-- seeded in 0002 — and signup is still Phase 3. What changes is only how a *browser*
-- proves it may talk to `/v1`: instead of pasting `MOTET_API_TOKEN` into a text field, the
-- owner signs in with Google and gets a session token that means the same thing. Nothing
-- here creates users, and `auth_sessions.user_id` references the one row that exists.
--
-- The email on the session is a *record of who signed in*, not an identity the system
-- resolves anything by. Authorization is `MOTET_ALLOWED_EMAILS`, checked in the API before
-- a session is ever minted, and unset means deny — see `motet_api.auth`.

-- --- the identity half of the OAuth handshake ----------------------------------------

-- 0003 wrote this check when Gmail was the only thing consent could be for. Sign-in is a
-- second one, against the *same* Google OAuth client: adding a value beats a second table
-- that would need its own single-use consume path, its own expiry sweep, and its own
-- chance of getting one of them wrong.
--
-- Dropped by name and *without* `IF EXISTS`: PostgreSQL names an unnamed column check
-- `<table>_<column>_check`, so if that assumption is ever wrong this fails here, loudly,
-- rather than adding a second constraint beside a first one that still refuses 'google'.
ALTER TABLE oauth_states DROP CONSTRAINT oauth_states_provider_check;
ALTER TABLE oauth_states ADD CONSTRAINT oauth_states_provider_check
    CHECK (provider IN ('gmail', 'google'));

-- OpenID Connect's replay defence, and it only means anything if it is stored: the value
-- goes out in the authorization request and comes back inside the signed ID token, so
-- checking it requires remembering what we sent. Nullable because the Gmail flow is plain
-- OAuth 2.0 and has no ID token to bind.
ALTER TABLE oauth_states ADD COLUMN nonce text;

-- --- browser sessions -----------------------------------------------------------------

-- What a signed-in browser holds instead of `MOTET_API_TOKEN`.
--
-- Server-side rows rather than a signed token, which is what makes **logout actually
-- revoke**: a self-contained JWT stays valid until it expires no matter how many times it
-- is "logged out", and there is nowhere to put a revocation list that is not this table
-- anyway. It also means no signing key to provision, rotate, or leak.
--
-- The token itself is never stored. `token_sha256` is a hash, because — unlike the feed
-- token, which the owner has to be able to read back onto a new device — nothing ever
-- needs to see this value again after it is handed to the browser that will present it.
CREATE TABLE auth_sessions (
    id           text        PRIMARY KEY,
    user_id      text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- Hex SHA-256 of the bearer token. Unique so that a collision is a write failure
    -- rather than two browsers sharing a session.
    token_sha256 text        NOT NULL UNIQUE,
    -- Which Google account signed in. Recorded so "who is this browser" is answerable —
    -- and because the allowlist is re-checked against it on *every* request, so taking an
    -- address off `MOTET_ALLOWED_EMAILS` destroys the sessions it already had rather than
    -- leaving them live for the rest of their thirty days.
    email        text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);

CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions (expires_at);
