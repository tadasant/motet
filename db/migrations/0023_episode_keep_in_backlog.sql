-- An episode whose listening does not touch read state.
--
-- Listening past a story marks it read (invariant 5), which is what empties the backlog
-- on a walk. An episode built from a hand-picked selection may be a preview rather than a
-- reading — "let me hear these, and still have them on the list" — so the owner asked for
-- a way to generate from items without consuming them.
--
-- A column on the episode rather than a flag on each news item, because it is a fact
-- about *this* listening: the same story heard in an ordinary episode is still read.
-- Default false is every episode that exists today, unchanged.
ALTER TABLE episodes
    ADD COLUMN keep_in_backlog boolean NOT NULL DEFAULT false;
