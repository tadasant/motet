"""Schema, migrations, and every SQL statement Motet runs.

Postgres holds the data *and* the job queue (``SKIP LOCKED``). There is no Redis and no
vector store — see the tripwires in AGENTS.md before adding either.

``repo`` is the single place queries live, shared by the API and the workers: read state,
the dedup window, and the episode state machine are all things two callers could define
slightly differently, and a slight difference in the definition of "unread" is how
invariant 5 stops being true.
"""

from . import allowlist, auth, phase2, repo
from .auth import AuthSession
from .migrations import MIGRATIONS_DIR, Migration, MigrationError, discover, migrate
from .models import (
    CredentialPurpose,
    EpisodeKind,
    EpisodeState,
    Highlight,
    IngestionStatus,
    SourceItemState,
    SourceKind,
    StoredClaim,
    StoredEpisode,
    StoredNewsItem,
    StoredSegment,
    StoredSource,
    StoredSourceItem,
)
from .repo import OWNER_USER_ID, PASTE_SOURCE_ID, connect
from .rules import DEFAULT_WINDOW_DAYS, MAX_WINDOW_DAYS, Ranking, RuleError, SmartRule

# `migrate` and `mint_session` are missing from the imports above deliberately, and that
# absence is structural rather than an oversight: each is executed as
# `python -m motet_db.<module>`, so a module the package has already pulled into
# `sys.modules` would be executed a second time under a second name, with a second copy of
# its module-level state. The runner is imported from `migrations` next door for exactly
# that reason. It shipped that way in `motet_workers.runner` (motet#21) and here
# (motet#27); `db/tests/test_db_entrypoints.py` fails if it recurs, and it checks every entry
# point in this package rather than only the two that exist today.
__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "MIGRATIONS_DIR",
    "OWNER_USER_ID",
    "PASTE_SOURCE_ID",
    "AuthSession",
    "CredentialPurpose",
    "EpisodeKind",
    "EpisodeState",
    "Highlight",
    "IngestionStatus",
    "Migration",
    "MigrationError",
    "Ranking",
    "RuleError",
    "SmartRule",
    "SourceItemState",
    "SourceKind",
    "StoredClaim",
    "StoredEpisode",
    "StoredNewsItem",
    "StoredSegment",
    "StoredSource",
    "StoredSourceItem",
    "allowlist",
    "auth",
    "connect",
    "discover",
    "migrate",
    "phase2",
    "repo",
]
