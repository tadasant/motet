-- What became of moving a Gmail message between labels when its owner ingested it
-- (motet#96).
--
-- Label sync is a best-effort write to the mailbox, made after the dedup transaction has
-- committed, and it never fails an ingest — so its outcome has nowhere to go but here. A
-- write-back that failed quietly would leave a newsletter sitting in `Newsletters` with no
-- trace of why, which is the never-infer-"no errors"-from-"no data" trap one stage along.
--
--   * `label_synced_at` — when the message's labels were changed. NULL means it has not
--     been, which is the normal state of every pasted item and of every mailbox that never
--     turned label sync on.
--   * `label_error` — why the last attempt did not change them. Cleared on success.
--   * `label_attempted_at` — when the last attempt, either way, was recorded. What lets the
--     Sources screen count only the failures since the mailbox was last authorized: a
--     "needs re-authorization" failure recorded before the owner re-consented is resolved
--     by that consent, and would otherwise sit under an "on" badge forever.
--
-- Three columns on an existing table used the way it is already used — `last_error` is the
-- same shape for the integrate stage — so no index: they are read per source item and per
-- source, off rows `source_items_user_idx` and the source's own items already find.
ALTER TABLE source_items ADD COLUMN label_synced_at timestamptz;
ALTER TABLE source_items ADD COLUMN label_error text;
ALTER TABLE source_items ADD COLUMN label_attempted_at timestamptz;
