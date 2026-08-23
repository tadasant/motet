-- The job queue. Postgres is the queue as well as the datastore — there is no Redis, and
-- adding one is a tripwire (see AGENTS.md). Workers claim with
-- `SELECT ... FOR UPDATE SKIP LOCKED`, which is why the ready-jobs index exists.
--
-- The pipeline stages (poll, extract, dedup/integrate, assemble, script, tts) are separate
-- queues on this one table: they have different rate limits and failure modes, so they are
-- drained independently rather than by one worker.

CREATE TABLE jobs (
    id          bigserial   PRIMARY KEY,
    queue       text        NOT NULL,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    state       text        NOT NULL DEFAULT 'ready'
                            CHECK (state IN ('ready', 'running', 'done', 'failed')),
    attempts    integer     NOT NULL DEFAULT 0,
    last_error  text,
    run_at      timestamptz NOT NULL DEFAULT now(),
    locked_at   timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Covers the claim query: ready jobs in one queue, due now, oldest first.
CREATE INDEX jobs_ready_idx ON jobs (queue, run_at, id) WHERE state = 'ready';
