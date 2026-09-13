-- Two tables for the admin screen's "Models & spend" block (motet#92). Both are
-- invariant-12 items — a new datastore role and a table used as a ledger — and AGENTS.md
-- records the owner's sign-off in "Models, spend, and the settings that only staging
-- honours".
--
-- `settings` is one string per key. The keys read today are `llm.model.<stage>` and
-- `llm.effort.<stage>` (`motet_inference.llm.config`), which sit *above* the environment:
-- settings > MOTET_LLM_*_<STAGE> > MOTET_LLM_* > default. **A row here is honoured only
-- where MOTET_SETTINGS_WRITABLE=1**, which a laptop and staging set and production never
-- does (`motet_db.settings`), so in production this table is inert whatever it holds and
-- the environment is the whole of the model configuration. A value is validated against
-- the committed catalogue when it is written and again when a worker reads it; it is
-- never trusted on read.
CREATE TABLE settings (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- `llm_usage` is one row per completion, appended by the worker from the same place the
-- `motet.llm.tokens` metric is recorded, and summed by the API. Write-only in the sense
-- that matters: nothing updates a row, and nothing but the retention sweep deletes one.
-- The metric carries no id by design and a log line cannot be summed from a route, so
-- this is the one shape that answers "what did this user cost this week".
--
-- No foreign keys, deliberately: a ledger row has to outlive the source item, episode or
-- job it is about, or deleting the subject would delete the record of what it cost.
-- `user_id` is resolved from the subject at insert time and stored, so the aggregate
-- never joins. `model` is the model the provider *says* it served, which can be a dated
-- snapshot of the configured slug. `reasoning_tokens` is the reasoning subset of
-- `output_tokens`, and the two cache figures are subsets of `input_tokens`, exactly as
-- OpenRouter reports them. `cache_ttl` is which rate the cache writes were billed at —
-- dedup caches for an hour and the script stage for five minutes, and the usage block
-- does not say which.
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
    cache_ttl          text        CHECK (cache_ttl IN ('5m', '1h')),
    user_id            text,
    subject            text,
    job_id             bigint
);

-- The retention sweep deletes by age in batches, oldest first, and the spend aggregate's
-- window is an age too; both are range scans on this.
CREATE INDEX llm_usage_occurred_at_idx ON llm_usage (occurred_at);
