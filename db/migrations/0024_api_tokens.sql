-- User-scoped personal access tokens: a second, non-interactive way to prove you may call
-- /v1, alongside the Google sign-in session that is the only way in today.
--
-- Asked for by Tadas, 2026-09-20: "Right now we can only log in via user interactive
-- oauth. Let's create functionality in motet to create 'app passwords' that bypass the
-- human oauth flow ... This is the mechanism we'll have the agent leverage when testing on
-- staging." Put the two shapes to him -- a staging-only shared secret, or a real token
-- system -- and he answered "Go straight to PATs." AGENTS.md, "A personal access token is
-- a third key to the same lock", records the alternatives and what a token may not do.
--
-- Tokens rather than passwords, which is the one place this departs from his words. Motet
-- has no password store at all -- sign-in is OAuth-only -- so "email + app password" would
-- mean introducing credential hashing, a reset flow and lockout logic the product needs
-- for nothing else. A bearer token has the same ergonomics for an agent and a far smaller
-- surface.
--
-- **Still one account.** `user_id` references the single `users.motet-owner` row migration
-- 0002 seeds, exactly as `auth_sessions` does. Nothing here is a user system.

CREATE TABLE api_tokens (
    id            text        PRIMARY KEY,
    user_id       text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,

    -- Hex SHA-256 of the whole token, and the only form of it that exists after the
    -- response that minted it. `auth_sessions.token_sha256`'s reason, one credential
    -- along: nothing ever needs the value back, so nothing keeps it. It is the primary
    -- lookup key so that verification is a single full-length index probe, which gives a
    -- timing measurement nothing partial to work with.
    token_sha256  text        NOT NULL UNIQUE,

    -- What the owner sees in the list, and what makes a leaked token identifiable at a
    -- glance and greppable in an incident: `mot_<environment>_<first 8 of the secret>`.
    -- Display only -- nothing authenticates against it -- and deliberately not enough of
    -- the secret to be worth anything (8 base64url characters off 256 bits).
    prefix        text        NOT NULL,

    -- The owner's own words for what this token is for. Free text, bounded by the route.
    label         text        NOT NULL,

    -- The allowlisted Google address of the session that minted it, re-checked on every
    -- request exactly as a session's is. De-listed has to mean gone, and a token that
    -- outlived its person would be the one credential the allowlist could not reach.
    email         text        NOT NULL,

    created_at    timestamptz NOT NULL DEFAULT now(),

    -- Coarse on purpose (see `motet_db.api_tokens.token_for_secret`): writing it on every
    -- request would take a row lock per call and serialize an agent's concurrent requests
    -- behind each other, which is the failure `auth_sessions.last_seen_at` already avoids.
    last_used_at  timestamptz,

    -- Optional at creation. Enforced in the lookup predicate rather than by a sweep, so a
    -- token that lapsed a second ago stops working now rather than when a job notices.
    expires_at    timestamptz,

    -- Revocation is a stamp, not a DELETE: "which tokens existed, and when did each stop"
    -- is the audit question, and with no database shell (invariant 10) this column is the
    -- only place it can be asked. The row stays in the list, marked.
    revoked_at    timestamptz
);

-- The list route reads every token for a user, newest first.
CREATE INDEX api_tokens_user_idx ON api_tokens (user_id, created_at DESC);
