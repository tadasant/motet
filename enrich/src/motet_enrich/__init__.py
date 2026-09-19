"""Motet's agentic enrichment service — the coding agent that fetches the article.

A newsletter is usually a preview and a link; the briefing should be made from the article
behind it. This package is the half of motet#102 that *does the fetching*: it is handed one
item, the links that item carried, the owner's login for that one site and whichever MCP
servers the owner connected, and it drives a coding agent over a stealth browser until it
has the article — or until one of its caps fires.

**It is a deployable of its own, and that is design option D2 rather than a packaging
preference.** The code it shells out to is third-party npm reading pages nobody at Motet
wrote, and any process in a Cloud Run container can mint the container's service-account
token; so this one runs under an identity with no project roles at all. Two properties
follow and both are asserted by tests rather than left to intention:

* **No database and no vault.** ``motet-db``, ``psycopg`` and ``motet-vault`` are not
  dependencies of this package and must not become ones — ``tests/test_no_database_reach.py``
  is what says so. The worker opens the credentials one run needs and sends them in the
  request; this service holds nothing at rest.
* **Nothing leaves here unredacted.** The transcript that comes back has been through
  :mod:`motet_enrich.redact`, whose first rule is that a non-browser tool's result — the
  mailbox search, the sign-in email — is never stored at all.
"""

from .config import EnrichSettings, Toolchain, load_settings, resolve_toolchain
from .contract import (
    EnrichHealth,
    EnrichRequest,
    EnrichResult,
    McpServer,
    RunCaps,
    RunStatus,
    SiteCredential,
    TranscriptEntry,
)
from .runner import FakeRunner, Runner, build_runner

__all__ = [
    "EnrichHealth",
    "EnrichRequest",
    "EnrichResult",
    "EnrichSettings",
    "FakeRunner",
    "McpServer",
    "RunCaps",
    "RunStatus",
    "Runner",
    "SiteCredential",
    "Toolchain",
    "TranscriptEntry",
    "build_runner",
    "load_settings",
    "resolve_toolchain",
]
