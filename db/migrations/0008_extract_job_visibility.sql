-- Find the extract jobs that have not finished, cheaply.
--
-- The sibling of migration 0005, one stage earlier. That one indexes the `integrate` job
-- a *source item* is joined to; this one indexes the `extract` job a source item does not
-- exist for yet. A Gmail message is polled, queued for extraction, and only becomes a
-- `source_items` row when extraction succeeds — so between those two moments the job row
-- is the only record that the message was ever seen, and a message whose extraction fails
-- five times leaves nothing else behind at all (motet#35).
--
-- `list_ingestion` therefore reads those rows directly, and it is read by a route the SPA
-- polls every few seconds. Without this index that read is a sequential scan of every job
-- ever run — nothing prunes `jobs`; `complete()` only flips the state to 'done' — so the
-- relation grows for the life of the deployment while the useful part of it does not.
--
-- Partial on both halves of that predicate, which is what keeps it small: only `extract`
-- jobs carry a `message_id`, and a job that is `done` is a message that made it and is
-- reported from its `source_items` row instead. What is left is the jobs in flight plus
-- the ones that failed, which is bounded by the mailbox rather than by history.
CREATE INDEX jobs_extract_open_idx ON jobs ((payload ->> 'source_id'))
    WHERE queue = 'extract' AND state <> 'done';
