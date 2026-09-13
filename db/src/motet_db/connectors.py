"""Connectors: the sites and remote MCP servers agentic enrichment may use (motet#102).

A ``site`` connector is one domain the owner has added, with an optional username and an
optional password. **Adding one is the opt-in** (option B3 of the design session): nothing
is fetched from a domain without a ``site`` row, so this table is the allowlist as well as
the credential store. An ``mcp`` connector is a remote MCP server the owner has authorized
over OAuth 2.1, handed to the enrichment agent as a tool source (option E1).

**The same rules as :mod:`motet_db.phase2`'s credential functions, and the same seam.**
Nothing here commits. A secret crosses this module in the clear at exactly two boundaries:
:func:`create_connector` and :func:`store_connector_secret` take a
:class:`~motet_vault.DekWrapper` and seal; :func:`load_connector_secret` takes a
:class:`~motet_vault.KeyManager` and opens — so the API, which holds the first and not the
second, can write a credential and cannot read one back (invariant 8).

**The AAD is ``user_id:connector_id:kind``.** It is built through :func:`motet_vault.aad`,
whose middle slot is named ``source_id`` because that is what it was written for; the
*string* has the same shape and the property is the same — a ciphertext moved onto another
connector row, or onto a row of the other kind, fails to authenticate rather than logging
one credential into another's site.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import psycopg
from motet_vault import DekWrapper, KeyManager, SealedSecret, aad, open_sealed, seal

from .ids import new_id
from .repo import _all, _maybe_one, _one

ConnectorKind = Literal["site", "mcp"]
ConnectorStatus = Literal["ready", "needs_auth", "error"]

SITE: Final = "site"
MCP: Final = "mcp"

#: What every read returns — everything on the row except the envelope. No function in this
#: module hands the sealed bytes to a caller.
_PUBLIC_COLUMNS: Final = """
    id, user_id, kind, label, domain, domains, url, username,
    (ciphertext IS NOT NULL) AS has_secret, secret_expires_at,
    oauth_issuer, oauth_client_id, oauth_token_endpoint, oauth_resource, oauth_redirect_uri,
    risk_acknowledged_at, status, last_error, created_at, updated_at
"""

#: A registrable host name: dot-separated labels of letters, digits and inner hyphens, and a
#: top-level label that is not all digits. An IP literal is refused because a site the owner
#: adds is a publication. **This is not an SSRF guard**: a host *name* can resolve inwards
#: (an internal metadata name passes this pattern), so whatever fetches from a site must
#: check the address it resolves to at fetch time, as `motet_sources.mcp_oauth` does.
_DOMAIN_RE: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


@dataclass(frozen=True)
class StoredConnector:
    """A connector as any caller may see it. Carries **no** secret and no envelope."""

    id: str
    user_id: str
    kind: str
    label: str
    domain: str | None
    domains: tuple[str, ...]
    url: str | None
    username: str | None
    has_secret: bool
    secret_expires_at: datetime | None
    oauth_issuer: str | None
    oauth_client_id: str | None
    oauth_token_endpoint: str | None
    oauth_resource: str | None
    oauth_redirect_uri: str | None
    risk_acknowledged_at: datetime | None
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def connector_aad(*, user_id: str, connector_id: str, kind: str) -> bytes:
    """``user_id:connector_id:kind`` — see the module docstring for the slot names."""
    return aad(user_id=user_id, source_id=connector_id, provider=kind)


def normalize_domain(raw: str) -> str:
    """``https://www.TheInformation.com/articles/x`` → ``theinformation.com``.

    Lowercase; scheme, userinfo, port, path, query and fragment stripped; a leading
    ``www.`` and stray dots dropped. An article's host is matched against this, so two
    spellings of one site must not become two rows — the unique index on
    ``(user_id, domain)`` is what enforces it, and this is what makes the index mean "one
    site" rather than "one string".
    """
    value = raw.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    value = value.split("@")[-1].split(":", 1)[0]
    value = value.strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


def is_valid_domain(domain: str) -> bool:
    """Whether an already-normalized value is a host name a site could live at."""
    return bool(_DOMAIN_RE.match(domain))


def domain_matches(host: str, domain: str) -> bool:
    """Whether ``host`` is ``domain`` or a subdomain of it.

    A click-tracking host such as ``url3396.example.com`` belongs to ``example.com``; a
    host that merely *ends* in the same letters (``notexample.com``) does not, which is why
    this compares on a dot boundary rather than with a bare ``endswith``.
    """
    host = normalize_domain(host)
    domain = normalize_domain(domain)
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


def create_connector(
    conn: psycopg.Connection[Any],
    wrapper: DekWrapper,
    *,
    user_id: str,
    kind: ConnectorKind,
    label: str,
    domain: str | None = None,
    domains: Sequence[str] = (),
    url: str | None = None,
    username: str | None = None,
    secret: str | None = None,
    risk_acknowledged: bool = False,
) -> StoredConnector:
    """Insert a connector, sealing ``secret`` if there is one.

    A ``site`` row starts ``ready`` whatever it carries: a site readable from the
    newsletter's own link needs no login, and one that emails a code needs only the
    address. An ``mcp`` row starts ``needs_auth`` — nothing about it works until consent
    has produced a token set, and the row exists first so the flow has something to bind
    its state to — and it cannot be written at all without ``risk_acknowledged``, which the
    table's own check enforces too.
    """
    if kind == MCP and not risk_acknowledged:
        raise ValueError("an MCP connector needs the owner's acknowledgement of its risk")
    connector_id = new_id("cn")
    sealed = (
        seal(
            wrapper,
            secret.encode(),
            connector_aad(user_id=user_id, connector_id=connector_id, kind=kind),
        )
        if secret
        else None
    )
    row = _one(
        conn,
        f"""
        INSERT INTO connectors
            (id, user_id, kind, label, domain, domains, url, username,
             ciphertext, nonce, wrapped_dek, backend, key_name, risk_acknowledged_at, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s THEN now() END, %s)
        RETURNING {_PUBLIC_COLUMNS}
        """,
        (
            connector_id,
            user_id,
            kind,
            label,
            domain,
            list(domains),
            url,
            username,
            sealed.ciphertext if sealed else None,
            sealed.nonce if sealed else None,
            sealed.wrapped_dek if sealed else None,
            sealed.backend if sealed else None,
            sealed.key_name if sealed else None,
            kind == MCP,
            "ready" if kind == SITE else "needs_auth",
        ),
    )
    return _connector(row)


def list_connectors(conn: psycopg.Connection[Any], user_id: str) -> list[StoredConnector]:
    rows = _all(
        conn,
        f"""
        SELECT {_PUBLIC_COLUMNS} FROM connectors
        WHERE user_id = %s ORDER BY kind DESC, created_at, id
        """,
        (user_id,),
    )
    return [_connector(row) for row in rows]


def get_connector(
    conn: psycopg.Connection[Any], connector_id: str, *, user_id: str
) -> StoredConnector | None:
    row = _maybe_one(
        conn,
        f"SELECT {_PUBLIC_COLUMNS} FROM connectors WHERE id = %s AND user_id = %s",
        (connector_id, user_id),
    )
    return _connector(row) if row else None


def delete_connector(conn: psycopg.Connection[Any], *, user_id: str, connector_id: str) -> bool:
    """Forget a connector and its secret. In-flight OAuth states for it cascade."""
    result = conn.execute(
        "DELETE FROM connectors WHERE id = %s AND user_id = %s", (connector_id, user_id)
    )
    return result.rowcount == 1


def set_connector_oauth_client(
    conn: psycopg.Connection[Any],
    connector_id: str,
    *,
    issuer: str,
    client_id: str,
    token_endpoint: str,
    resource: str,
    redirect_uri: str,
) -> None:
    """Record the client that issued a grant, so a refresh can skip discovery.

    Called when consent completes, beside :func:`store_connector_secret`, never when it
    starts — so these fields always describe the grant that is sealed on the row.
    """
    conn.execute(
        """
        UPDATE connectors
        SET oauth_issuer = %s, oauth_client_id = %s, oauth_token_endpoint = %s,
            oauth_resource = %s, oauth_redirect_uri = %s, updated_at = now()
        WHERE id = %s
        """,
        (issuer, client_id, token_endpoint, resource, redirect_uri, connector_id),
    )


def set_connector_status(
    conn: psycopg.Connection[Any],
    connector_id: str,
    *,
    status: ConnectorStatus,
    last_error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE connectors SET status = %s, last_error = %s, updated_at = now() WHERE id = %s",
        (status, last_error, connector_id),
    )


def store_connector_secret(
    conn: psycopg.Connection[Any],
    wrapper: DekWrapper,
    *,
    connector_id: str,
    secret: str,
    expires_at: datetime | None = None,
) -> StoredConnector:
    """Seal a secret onto an existing connector and mark it ready.

    The AAD is rebuilt from the row's own ``user_id`` and ``kind``, never from the caller,
    for the same reason :func:`load_connector_secret` does it: the binding is only worth
    having if the row is the authority on what it is bound to.
    """
    identity = _one(conn, "SELECT user_id, kind FROM connectors WHERE id = %s", (connector_id,))
    sealed = seal(
        wrapper,
        secret.encode(),
        connector_aad(
            user_id=identity["user_id"], connector_id=connector_id, kind=identity["kind"]
        ),
    )
    row = _one(
        conn,
        f"""
        UPDATE connectors
        SET ciphertext = %s, nonce = %s, wrapped_dek = %s, backend = %s, key_name = %s,
            secret_expires_at = %s, status = 'ready', last_error = NULL, updated_at = now()
        WHERE id = %s
        RETURNING {_PUBLIC_COLUMNS}
        """,
        (
            sealed.ciphertext,
            sealed.nonce,
            sealed.wrapped_dek,
            sealed.backend,
            sealed.key_name,
            expires_at,
            connector_id,
        ),
    )
    return _connector(row)


def load_connector_secret(
    conn: psycopg.Connection[Any], manager: KeyManager, *, connector_id: str
) -> str | None:
    """Open a connector's secret. **Workers only** — this is the decrypt half.

    ``None`` when the connector exists without a secret (a site with no password, an MCP
    server not authorized yet) as well as when it does not exist; a caller that cares about
    the difference already has the row.
    """
    row = _maybe_one(
        conn,
        "SELECT user_id, kind, ciphertext, nonce, wrapped_dek FROM connectors WHERE id = %s",
        (connector_id,),
    )
    if row is None or row["ciphertext"] is None:
        return None
    sealed = SealedSecret(
        ciphertext=bytes(row["ciphertext"]),
        nonce=bytes(row["nonce"]),
        wrapped_dek=bytes(row["wrapped_dek"]),
        backend="",
        key_name="",
    )
    plaintext = open_sealed(
        manager,
        sealed,
        connector_aad(user_id=row["user_id"], connector_id=connector_id, kind=row["kind"]),
    )
    return plaintext.decode()


def _connector(row: dict[str, Any]) -> StoredConnector:
    return StoredConnector(
        id=row["id"],
        user_id=row["user_id"],
        kind=row["kind"],
        label=row["label"],
        domain=row["domain"],
        domains=tuple(row["domains"] or ()),
        url=row["url"],
        username=row["username"],
        has_secret=bool(row["has_secret"]),
        secret_expires_at=row["secret_expires_at"],
        oauth_issuer=row["oauth_issuer"],
        oauth_client_id=row["oauth_client_id"],
        oauth_token_endpoint=row["oauth_token_endpoint"],
        oauth_resource=row["oauth_resource"],
        oauth_redirect_uri=row["oauth_redirect_uri"],
        risk_acknowledged_at=row["risk_acknowledged_at"],
        status=row["status"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "MCP",
    "SITE",
    "ConnectorKind",
    "ConnectorStatus",
    "StoredConnector",
    "connector_aad",
    "create_connector",
    "delete_connector",
    "domain_matches",
    "get_connector",
    "is_valid_domain",
    "list_connectors",
    "load_connector_secret",
    "normalize_domain",
    "set_connector_oauth_client",
    "set_connector_status",
    "store_connector_secret",
]
