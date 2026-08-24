"""Schema, migrations, and every SQL statement Motet runs.

Postgres holds the data *and* the job queue (``SKIP LOCKED``). There is no Redis and no
vector store — see the tripwires in AGENTS.md before adding either.

``repo`` is the single place queries live, shared by the API and the workers: read state,
the dedup window, and the episode state machine are all things two callers could define
slightly differently, and a slight difference in the definition of "unread" is how
invariant 5 stops being true.
"""

from . import repo
from .migrate import MIGRATIONS_DIR, Migration, MigrationError, discover, migrate
from .models import (
    EpisodeState,
    SourceItemState,
    StoredClaim,
    StoredEpisode,
    StoredNewsItem,
    StoredSegment,
    StoredSourceItem,
)
from .repo import OWNER_USER_ID, PASTE_SOURCE_ID, connect

__all__ = [
    "MIGRATIONS_DIR",
    "OWNER_USER_ID",
    "PASTE_SOURCE_ID",
    "EpisodeState",
    "Migration",
    "MigrationError",
    "SourceItemState",
    "StoredClaim",
    "StoredEpisode",
    "StoredNewsItem",
    "StoredSegment",
    "StoredSourceItem",
    "connect",
    "discover",
    "migrate",
    "repo",
]
