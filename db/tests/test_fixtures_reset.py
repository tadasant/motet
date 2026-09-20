"""The reset classifies every table, and the two lists it reports are true of the schema.

`RESET_KEEPS` is reported on the wire as *"tables a reset never touches"*, which is a claim
a caller asserting a baseline is entitled to read as exhaustive. Nothing made it so: the
first version was silently short by four tables that are in fact kept, and a table added by
a future migration would fall into neither list with no test to notice.

So the property pinned here is **classification**, not contents: every table the migrations
create is either deleted by :data:`~motet_db.fixtures._RESET_STEPS` or listed in
:data:`~motet_db.fixtures.RESET_KEEPS`, and no entry names a table that does not exist. A
new table is then a red run and a decision, which is the same shape as
``api/tests/test_mcp_parity.py``'s rule for a new route.

Read off the **live database** rather than by parsing the migration files, because that is
what the reset runs against — a table renamed in a later migration is a real row in
``information_schema`` and a stale string in a `.sql` file.
"""

from __future__ import annotations

from typing import Any

import psycopg
from motet_db import fixtures, repo

#: Bookkeeping the migration runner owns, which is not user data and is nobody's to classify.
NOT_USER_DATA = frozenset({"schema_migrations"})


def _tables(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """
    ).fetchall()
    return {str(row["table_name"]) for row in rows} - NOT_USER_DATA


def _deleted() -> set[str]:
    return {table for table, _ in fixtures._RESET_STEPS}


def test_every_table_is_either_reset_or_deliberately_kept(db: psycopg.Connection[Any]) -> None:
    classified = _deleted() | set(fixtures.RESET_KEEPS)
    unclassified = sorted(_tables(db) - classified)
    assert not unclassified, (
        f"tables the reset neither deletes nor lists as kept: {unclassified}. Add each to "
        "_RESET_STEPS or to RESET_KEEPS with the reason it survives — a caller reads "
        "`kept` as the whole of what a reset leaves standing."
    )


def test_neither_list_names_a_table_that_does_not_exist(db: psycopg.Connection[Any]) -> None:
    dangling = sorted((_deleted() | set(fixtures.RESET_KEEPS)) - _tables(db))
    assert not dangling, f"the reset names tables the schema does not have: {dangling}"


def test_no_table_is_both_deleted_and_kept() -> None:
    both = sorted(_deleted() & set(fixtures.RESET_KEEPS))
    assert not both, f"listed as both deleted and kept: {both}"


def test_jobs_are_deleted_before_anything_their_payload_resolves_through() -> None:
    """The ordering the reset's correctness rests on, as an assertion rather than a comment.

    A job is resolved to a user by joining its payload to the row it is about, so deleting
    ``source_items``, ``episodes`` or ``sources`` first leaves its jobs resolving to nobody —
    unmatched by the delete, claimed by the next drain, failing against a row that is gone,
    and retained in the failure counts for ninety days.
    """
    order = [table for table, _ in fixtures._RESET_STEPS]
    assert order[0] == "jobs", f"jobs must be deleted first; the order is {order}"


def test_the_seeded_paste_source_survives_a_reset(db: psycopg.Connection[Any]) -> None:
    """Deleting it takes paste-in down in a way that reads as an application bug.

    Asserted against a real reset rather than by reading the SQL, because the exemption is a
    ``WHERE`` clause and a typo in it is invisible to a test of the statement's text.
    """
    repo.insert_source_item(db, user_id=repo.OWNER_USER_ID, title="Acme", text="Acme raised $20M.")
    db.commit()

    removed = fixtures.reset_user(db, user_id=repo.OWNER_USER_ID)
    db.commit()

    assert removed["source_items"] == 1
    assert removed["sources"] == 0, "there was no source to remove but the seeded one"
    surviving = {str(row["id"]) for row in db.execute("SELECT id FROM sources").fetchall()}
    assert surviving == {repo.PASTE_SOURCE_ID}
