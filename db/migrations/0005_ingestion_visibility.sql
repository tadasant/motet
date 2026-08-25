-- Look a source item's ingestion job back up, cheaply.
--
-- The backlog screen asks "what have I pasted that is not a news item yet, and why not?".
-- The *why* lives on the job row — `attempts`, `run_at`, `last_error` — because a source
-- item that is still being retried has not failed yet and carries no error of its own.
-- Postgres being the queue as well as the datastore (see the AGENTS.md tripwires) is what
-- makes that a join rather than a second system to ask.
--
-- Without this index that join is a sequential scan of every job ever run, on a query the
-- SPA polls while anything is pending. Partial and expression-based: only `integrate`
-- jobs carry a `source_item_id`, so only those rows are worth indexing.
CREATE INDEX jobs_source_item_idx ON jobs ((payload ->> 'source_item_id'))
    WHERE queue = 'integrate';
