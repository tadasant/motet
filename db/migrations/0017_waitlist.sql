-- The waitlist the getmotet.com landing page collects (`POST /v1/waitlist`), read back on
-- the admin screen (`GET /v1/admin/waitlist`).
--
-- Deliberately not a `users` row. Signup is still out of scope and `users` holds the one
-- seeded account; an address on this list is somebody asking to be told, not an identity
-- that may do anything. Nothing references this table and it references nothing.
--
-- One row per address. The address is stored as the API normalized it (trimmed and
-- lowercased), so the unique constraint is what makes a second submission of the same
-- address an update rather than a second row — the idempotence the route promises, held by
-- the database rather than by a read-then-write that two tabs could race.
--
-- `submissions` and `last_submitted_at` are what a repeat leaves behind. They are for an
-- operator reading the list ("this person asked three times"), never for the response: the
-- route answers a new address and a known one identically, so it cannot be used to learn
-- who is already on it.
CREATE TABLE waitlist_signups (
    id                bigserial PRIMARY KEY,
    email             text        NOT NULL UNIQUE,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_submitted_at timestamptz NOT NULL DEFAULT now(),
    submissions       integer     NOT NULL DEFAULT 1,
    CONSTRAINT waitlist_signups_email_normalized
        CHECK (email = lower(btrim(email)) AND length(email) BETWEEN 3 AND 254)
);
