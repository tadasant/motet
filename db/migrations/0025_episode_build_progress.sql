-- Where an episode is between "make it" and a file to play, cheaply, and with a count.
--
-- Creating an episode returns in `pending` and the rest happens on three queues —
-- `assemble`, `script`, `tts` — so a screen that has only `episodes.state` can say which
-- stage the episode is at and nothing else: not whether a worker has it, not whether the
-- stage is on its fourth attempt, and not how far through the longest one it is. That is
-- motet#136's Gmail complaint one surface along, and this migration is the two facts the
-- API needs to answer it.

-- --------------------------------------------------------------------------------------
-- How many of this render's segments have been synthesized.
-- --------------------------------------------------------------------------------------
--
-- TTS is the only stage with an inside worth reporting: it is a loop over segments, it is
-- the slowest and the most expensive, and — unlike dedup or scripting, which are one model
-- call each — a count part-way through it is a real number rather than a guess. The worker
-- writes it on a side connection as each segment comes back, because its own transaction is
-- invisible for as long as the render holds it (the same reason `enrich_status = 'running'`
-- is written on one, motet#102).
--
-- On the episode rather than on the job row: a re-render is a different job and the same
-- episode, and the screen asks about the episode. `handle_tts` sets it to 0 when a render
-- starts, so a retry counts up from nothing rather than resuming somebody else's tally.
--
-- Nothing downstream reads it: the audio, the durations and the claim timings are all still
-- written from what TTS returned. It is a progress counter and only that, which is why a
-- write that fails is logged and swallowed rather than failing the render.
ALTER TABLE episodes
    ADD COLUMN rendered_segments integer NOT NULL DEFAULT 0;

-- --------------------------------------------------------------------------------------
-- Find an episode's pipeline job, cheaply.
-- --------------------------------------------------------------------------------------
--
-- Migration 0005's sibling, on the other half of the pipeline. That one indexes the
-- `integrate` job a *source item* is joined to so the ingestion panel is not a sequential
-- scan; this one does the same for the `assemble`/`script`/`tts` job an *episode* is joined
-- to, on a route the SPA polls every three seconds while anything is being made and the
-- phone polls every two.
--
-- The reason it is a join at all is 0005's reason exactly: the episode row says which stage
-- it is at, and only the job row knows whether a worker has it, which attempt it is on, and
-- what the last attempt said. An episode still climbing the retry ladder has no `last_error`
-- of its own — `episode_failed` writes that only once the attempts run out — so a view built
-- from `episodes` alone cannot tell "a worker is on it" from "it failed four times and is
-- waiting to try again".
--
-- Partial on both halves, and each earns its place differently. The queue list is needed
-- because the key is not exclusive to these three: nothing else carries an `episode_id`
-- today, but the predicate is what keeps that true of the index rather than of the moment.
-- `state <> 'done'` is the half that keeps it small — every episode ever made leaves three
-- `done` rows behind, and the query never wants one: an episode whose stage finished is
-- described by the *next* stage's row, or by being `ready`. What is left is the jobs in
-- flight plus the ones that failed, and migration 0010 bounds both.
CREATE INDEX jobs_episode_idx ON jobs ((payload ->> 'episode_id'))
    WHERE queue IN ('assemble', 'script', 'tts') AND state <> 'done';
