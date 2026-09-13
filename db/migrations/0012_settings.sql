-- PROTOTYPE (proto/local-ux): runtime-mutable LLM configuration and a per-completion
-- spend ledger, both for the /admin screen.
--
-- Two new tables, and both are invariant-12 items for the real version — a new datastore
-- role (a key/value settings store; a table used as a ledger) rather than a column on an
-- existing table used the way it already is. This migration exists so the prototype can be
-- exercised end to end; the design session decides whether either survives.
--
-- `settings` holds one string per key. The keys the application reads today are
-- `llm.model.<stage>` and `llm.effort.<stage>` (see `motet_inference.llm.config`), which
-- sit *above* the environment in precedence: settings > MOTET_LLM_*_<STAGE> > MOTET_LLM_*
-- > default. A key nobody reads is harmless; a value is validated against the model
-- catalogue when it is written (400 on an unknown slug), never trusted on read.
CREATE TABLE settings (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- `llm_usage` is one row per completion, written by the worker from the same place the
-- `motet.llm.tokens` metric is recorded. Until now usage was a metric (no ids, by design)
-- and a log line (ids, but not queryable) — this is the shape that answers "what did this
-- user cost this week" from a route rather than from Grafana.
--
-- No foreign keys, deliberately: a ledger row must outlive the source item, episode or job
-- it is about, or deleting a row deletes the record of what it cost. `user_id` is resolved
-- from the subject at insert time and stored, so the aggregate never has to join.
-- `reasoning_tokens` is the reasoning *subset* of `output_tokens`, as OpenRouter reports
-- it; `cache_read_tokens` and `cache_write_tokens` are subsets of `input_tokens`.
CREATE TABLE llm_usage (
    id                 bigserial   PRIMARY KEY,
    occurred_at        timestamptz NOT NULL DEFAULT now(),
    stage              text        NOT NULL,
    model              text        NOT NULL,
    input_tokens       integer     NOT NULL DEFAULT 0,
    output_tokens      integer     NOT NULL DEFAULT 0,
    reasoning_tokens   integer     NOT NULL DEFAULT 0,
    cache_read_tokens  integer     NOT NULL DEFAULT 0,
    cache_write_tokens integer     NOT NULL DEFAULT 0,
    user_id            text,
    subject            text,
    job_id             bigint
);

-- The overview aggregates by stage and by user over the whole table; the one filter it
-- will grow is a time window.
CREATE INDEX llm_usage_occurred_at_idx ON llm_usage (occurred_at);
