"""Schema, migrations, and every SQL statement Motet runs.

Postgres holds the data *and* the job queue (``SKIP LOCKED``). There is no Redis and no
vector store — see the tripwires in AGENTS.md before adding either.

``repo`` is the single place queries live, shared by the API and the workers: read state,
the dedup window, and the episode state machine are all things two callers could define
slightly differently, and a slight difference in the definition of "unread" is how
invariant 5 stops being true.
"""

from . import auth, phase2, repo
from .auth import AuthSession
from .migrate import MIGRATIONS_DIR, Migration, MigrationError, discover, migrate
from .models import (
    CredentialPurpose,
    EpisodeKind,
    EpisodeState,
    Highlight,
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
    "auth",
    "connect",
    "discover",
    "migrate",
    "phase2",
    "repo",
]
