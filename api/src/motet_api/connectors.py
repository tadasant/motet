"""The Credentials screen's API half: what a connector request may say, and the answer.

motet#102. The routes live in ``main.py`` beside the sources routes (see the note on
``/v1/admin`` there for why this repo does not use an ``APIRouter``); this module is what
they call. Four things about it are the design rather than the implementation:

* **No answer ever carries a secret.** :func:`connector_response` reports ``has_secret``
  and nothing about what it is, and the repository functions it reads from cannot return
  the envelope either. The API holds the vault's encrypt-only half (invariant 8).
* **A site needs only a domain.** Adding a site is the owner's opt-in to fetching articles
  from it (option B3); a username and a password are for the sites that need a login, and
  a password with no username is refused because nobody could log in with it.
* **An MCP server cannot be added without the owner acknowledging its risk** (option E1,
  kept "with the risk made clear when connecting"). The screen shows :data:`MCP_RISK` and a
  checkbox; this module refuses the request without ``acknowledge_risk`` so that a client
  which never rendered the warning cannot skip it, and the row records when it was given.
* **The consent comes back on the SPA's one callback path**, and the ``connector.`` state
  prefix is what routes it to ``/v1/connectors/oauth/callback`` rather than to the mailbox
  or sign-in flows. Keep :data:`CONNECTOR_STATE_PREFIX` in step with ``web/src/oauth.ts``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from motet_db.connectors import (
    MCP,
    SITE,
    ConnectorKind,
    StoredConnector,
    is_valid_domain,
    normalize_domain,
)

from .schemas import ConnectorResponse, CreateConnectorRequest

#: Marks a ``state`` as belonging to an MCP connector's authorization — the third flow on
#: the SPA's one callback path. The dot is a safe marker for ``LOGIN_STATE_PREFIX``'s
#: reason: ``secrets.token_urlsafe`` never emits one.
CONNECTOR_STATE_PREFIX: Final = "connector."

#: The warning an MCP connector is added under. The screen carries the same sentences.
MCP_RISK: Final = (
    "Connecting an MCP server hands its tools to the enrichment agent, and that agent also "
    "reads web pages nobody at Motet wrote. A hostile page can instruct it to use this "
    "server with your account — to read what the server can see and carry it somewhere "
    "else. Motet's safeguards narrow that; they do not close it. Confirm you understand "
    "this before connecting a server."
)


class ConnectorInputError(ValueError):
    """A create request that cannot become a connector. The message is the 400's detail."""


@dataclass(frozen=True)
class ConnectorSpec:
    """A validated create request, in the repository's terms."""

    kind: ConnectorKind
    label: str
    domain: str | None = None
    domains: tuple[str, ...] = field(default_factory=tuple)
    url: str | None = None
    username: str | None = None
    secret: str | None = None
    risk_acknowledged: bool = False


def connector_spec(body: CreateConnectorRequest) -> ConnectorSpec:
    """Normalize and check a create request, or say in a sentence why it cannot be one."""
    label = body.label.strip()
    if body.kind == SITE:
        domain = normalize_domain(body.domain or "")
        if not is_valid_domain(domain):
            raise ConnectorInputError(
                "A site needs a domain such as example.com — a host name, not an address."
            )
        username = (body.username or "").strip() or None
        password = body.password or None
        if password and username is None:
            raise ConnectorInputError("A password needs the username it belongs to.")
        return ConnectorSpec(
            kind=SITE,
            label=label or domain,
            domain=domain,
            username=username,
            secret=password,
        )

    url = (body.url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ConnectorInputError("An MCP server URL must be an https URL.")
    if not body.acknowledge_risk:
        raise ConnectorInputError(MCP_RISK)
    domains: list[str] = []
    for raw in body.domains:
        domain = normalize_domain(raw)
        if not domain:
            continue
        if not is_valid_domain(domain):
            raise ConnectorInputError(f"{raw!r} is not a domain such as example.com.")
        if domain not in domains:
            domains.append(domain)
    return ConnectorSpec(
        kind=MCP,
        label=label or parts.hostname,
        domains=tuple(domains),
        url=url,
        risk_acknowledged=True,
    )


def new_connector_state() -> str:
    """The CSRF token for one connector authorization, carrying the flow's prefix."""
    return f"{CONNECTOR_STATE_PREFIX}{secrets.token_urlsafe(32)}"


def is_connector_state(state: str) -> bool:
    return state.startswith(CONNECTOR_STATE_PREFIX)


def connector_response(connector: StoredConnector) -> ConnectorResponse:
    return ConnectorResponse(
        id=connector.id,
        kind=MCP if connector.kind == MCP else SITE,
        label=connector.label,
        domain=connector.domain,
        domains=list(connector.domains),
        url=connector.url,
        username=connector.username,
        has_secret=connector.has_secret,
        secret_expires_at=connector.secret_expires_at,
        oauth_issuer=connector.oauth_issuer,
        oauth_registered=connector.oauth_client_id is not None,
        risk_acknowledged_at=connector.risk_acknowledged_at,
        status=connector.status,  # type: ignore[arg-type]  # the table's CHECK is the Literal
        last_error=connector.last_error,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


__all__ = [
    "CONNECTOR_STATE_PREFIX",
    "MCP_RISK",
    "ConnectorInputError",
    "ConnectorSpec",
    "connector_response",
    "connector_spec",
    "is_connector_state",
    "new_connector_state",
]
