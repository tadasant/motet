-- Answer "has this mailbox message ever been queued for extraction?", cheaply.
--
-- `handle_poll` asks it for every page it lists (`phase2.unqueued_message_ids`): Gmail
-- listing is a watermarked search that deliberately overlaps its previous pass (motet#94,
-- motet#95), so a poll re-lists messages it has already handed on, and one that extraction
-- skipped — a receipt, an invite — has no `source_items` row, only a finished extract job.
-- The lookup therefore has to see extract jobs in *every* state, `done` included, which
-- migration 0008's index deliberately excludes.
--
-- Keyed on the message id alone, and that is deliberate. A provider's message id is all but
-- unique on its own, so it is what makes the lookup cheap; `source_id` is then a filter on
-- a row or two. Leading with `source_id` instead would make this index a better match than
-- 0008's for `list_ingestion`'s extract arm — the planner takes it, and that arm walks every
-- extract job a mailbox has in the retention window, `done` included, rather than the
-- handful 0008 keeps. Partial on `queue = 'extract'` because only extract jobs carry one.
CREATE INDEX jobs_extract_message_idx ON jobs ((payload ->> 'message_id'))
    WHERE queue = 'extract';
