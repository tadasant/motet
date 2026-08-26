-- "Is anything actually draining the queue?" — a question the system could not answer.
--
-- The SPA told the user a worker would take a pasted item off the queue within a few
-- seconds. Nothing did, because nothing was running (motet#38), and no surface anywhere
-- could tell the difference between a busy worker and no worker at all. A queued item
-- looks identical either way, so the failure was silent and read as slowness.
--
-- One row per queue, written at the top of every drain pass. That makes "no worker has
-- run in twenty minutes" a fact the API can report rather than a guess the client makes
-- from an item's age — and it keeps the two apart, which is the never-infer-"no errors"-
-- from-"no data" rule in AGENTS.md applied to the queue instead of to telemetry.
--
-- Deliberately NOT a job-queue row and NOT per-process. It answers one question about the
-- deployment, so a lease, a worker id, or a history of passes would all be state nothing
-- reads. `ON CONFLICT DO UPDATE` on a handful of rows costs a page write per pass.
CREATE TABLE worker_heartbeats (
    queue        text        PRIMARY KEY,
    last_seen_at timestamptz NOT NULL
);
