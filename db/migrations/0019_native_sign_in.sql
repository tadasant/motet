-- The iOS app signs in through the web sign-in, and a one-time handoff code carries the
-- result back to the app.
--
-- Chosen by Tadas, 2026-09-13, as option A of the options put to him in Zimmer session
-- 17805: reuse the SPA's Google sign-in inside the app's system sign-in sheet rather than
-- register a second (iOS) Google OAuth client, and rather than keep pasting
-- `MOTET_API_TOKEN` into the phone. AGENTS.md, "The phone signs in through the web
-- sign-in", records the alternatives.
--
-- The flow, and what each piece below is for:
--
--   1. The app makes a PKCE pair and asks `POST /v1/auth/native/start` with the challenge.
--      The API records an ordinary `oauth_states` row for a Google sign-in whose redirect
--      is the SPA's own registered `/oauth/callback`, plus the app's challenge.
--   2. Google returns the in-app browser to the SPA, which finishes the sign-in exactly as
--      a browser does. Because the pending row carries a challenge, the API mints no session
--      there: it stores a handoff and answers with a `motet://` link carrying its code.
--   3. The app redeems the code *with its verifier* at `POST /v1/auth/native/redeem`, and
--      only then is a session minted.
--
-- The session token itself therefore never travels in a URL. What does is a code that is
-- single-use, expires in two minutes, and is worthless without the verifier that never
-- left the app — so a code read by another app that registered the same URL scheme buys
-- that app nothing.

-- The app's PKCE challenge, on the pending authorization it belongs to. NULL for every
-- browser sign-in and every mailbox connection, which is what keeps those flows unchanged.
ALTER TABLE oauth_states ADD COLUMN handoff_challenge text;

-- A verified, allowlisted sign-in waiting for the app that started it to collect it.
--
-- Its own table rather than more columns on `oauth_states`, because that row is consumed
-- by the callback (`DELETE ... RETURNING`) and the handoff has to outlive it by one more
-- round trip. Only the code's hash is stored, for `auth_sessions.token_sha256`'s reason:
-- nothing needs to read the code back once the link carrying it has been handed out.
CREATE TABLE auth_handoffs (
    code_sha256    text        PRIMARY KEY,
    user_id        text        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- The Google account the callback verified and found on the allowlist. The allowlist
    -- is checked again at redeem, so an address removed in between gets nothing.
    email          text        NOT NULL,
    -- base64url(SHA-256(verifier)), as the app sent it to /v1/auth/native/start.
    code_challenge text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL
);

CREATE INDEX auth_handoffs_expiry_idx ON auth_handoffs (expires_at);
