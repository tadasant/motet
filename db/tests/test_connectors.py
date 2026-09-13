"""Connectors through the repository layer and the local vault (motet#102).

What is pinned: a site with no login at all is a legal, ready row — adding a site is the
opt-in, and a site readable from the newsletter's link needs nothing more; a sealed secret
round-trips under ``user_id:connector_id:kind`` and fails to open under any other row's
identity; no read returns the envelope; an MCP row cannot exist without the owner's
acknowledgement of its risk; one site row per domain; and deleting a connector takes its
in-flight OAuth state with it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
import pytest
from motet_db import connectors, phase2, repo
from motet_vault import DecryptionError, LocalKeyManager

USER = repo.OWNER_USER_ID


@pytest.fixture
def key() -> LocalKeyManager:
    return LocalKeyManager(kek=hashlib.sha256(b"connectors-test-kek").digest())


def test_a_site_with_no_login_is_a_complete_ready_row(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db, key, user_id=USER, kind="site", label="Example", domain="example.com"
    )
    assert created.status == "ready"
    assert created.username is None and created.has_secret is False
    assert created.risk_acknowledged_at is None
    assert connectors.load_connector_secret(db, key, connector_id=created.id) is None
    assert [c.id for c in connectors.list_connectors(db, USER)] == [created.id]


def test_a_passwordless_login_is_a_username_alone(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="site",
        label="Example",
        domain="example.com",
        username="reader@example.net",
    )
    assert created.username == "reader@example.net" and created.has_secret is False


def test_a_password_without_a_username_is_refused_by_the_table(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        connectors.create_connector(
            db, key, user_id=USER, kind="site", label="x", domain="x.example", secret="pw"
        )


def test_a_sealed_secret_round_trips_and_is_bound_to_its_row(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    a = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="site",
        label="a",
        domain="a.example",
        username="u",
        secret="pw-a",
    )
    b = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="site",
        label="b",
        domain="b.example",
        username="u",
        secret="pw-b",
    )
    assert a.has_secret and b.has_secret
    assert connectors.load_connector_secret(db, key, connector_id=a.id) == "pw-a"

    # Move a's envelope onto b: the AAD names b's id, so the ciphertext must not open.
    db.execute(
        """
        UPDATE connectors
        SET ciphertext = s.ciphertext, nonce = s.nonce, wrapped_dek = s.wrapped_dek
        FROM (SELECT ciphertext, nonce, wrapped_dek FROM connectors WHERE id = %s) AS s
        WHERE connectors.id = %s
        """,
        (a.id, b.id),
    )
    with pytest.raises(DecryptionError):
        connectors.load_connector_secret(db, key, connector_id=b.id)


def test_the_aad_is_user_connector_kind() -> None:
    assert connectors.connector_aad(user_id="u", connector_id="cn_1", kind="mcp") == b"u:cn_1:mcp"


def test_an_mcp_connector_needs_the_risk_acknowledged(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    with pytest.raises(ValueError, match="acknowledgement"):
        connectors.create_connector(
            db, key, user_id=USER, kind="mcp", label="m", url="https://mcp.example/mcp"
        )
    # And the table says the same thing to a caller that skips the function's check.
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            """
            INSERT INTO connectors (id, user_id, kind, label, url, status)
            VALUES ('cn_raw', %s, 'mcp', 'm', 'https://mcp.example/mcp', 'needs_auth')
            """,
            (USER,),
        )


def test_an_mcp_token_set_seals_onto_an_existing_row(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="mcp",
        label="Mail",
        url="https://mcp.example/mcp?servers=x",
        risk_acknowledged=True,
    )
    assert created.status == "needs_auth" and created.risk_acknowledged_at is not None
    tokens = json.dumps({"access_token": "at", "refresh_token": "rt"})
    stored = connectors.store_connector_secret(db, key, connector_id=created.id, secret=tokens)
    assert stored.status == "ready" and stored.has_secret
    opened = connectors.load_connector_secret(db, key, connector_id=created.id)
    assert json.loads(opened or "")["refresh_token"] == "rt"


def test_no_read_carries_the_envelope(db: psycopg.Connection[Any], key: LocalKeyManager) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="site",
        label="a",
        domain="a.example",
        username="u",
        secret="pw-z",
    )
    for record in (
        created,
        connectors.get_connector(db, created.id, user_id=USER),
        *connectors.list_connectors(db, USER),
    ):
        assert record is not None
        assert not any(name in vars(record) for name in ("ciphertext", "nonce", "wrapped_dek"))
        assert "pw-z" not in repr(record)


def test_another_users_connector_is_not_theirs_to_read_or_delete(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db, key, user_id=USER, kind="site", label="a", domain="a.example"
    )
    assert connectors.get_connector(db, created.id, user_id="someone-else") is None
    assert connectors.delete_connector(db, user_id="someone-else", connector_id=created.id) is False


def test_one_site_row_per_domain(db: psycopg.Connection[Any], key: LocalKeyManager) -> None:
    connectors.create_connector(db, key, user_id=USER, kind="site", label="a", domain="a.example")
    with pytest.raises(psycopg.errors.UniqueViolation):
        connectors.create_connector(
            db, key, user_id=USER, kind="site", label="again", domain="a.example", username="v"
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("https://www.Example.com/articles/x?y=1", "example.com"),
        ("http://user@Host.Example:8443/", "host.example"),
        ("  www.example.com.  ", "example.com"),
    ],
)
def test_domains_normalize_to_a_bare_host(raw: str, expected: str) -> None:
    assert connectors.normalize_domain(raw) == expected


@pytest.mark.parametrize(
    ("domain", "valid"),
    [
        ("example.com", True),
        ("news.example.co.uk", True),
        ("nodot", False),
        ("127.0.0.1", False),
        ("exa mple.com", False),
        ("-bad.example", False),
        ("", False),
    ],
)
def test_only_a_host_name_is_a_site(domain: str, valid: bool) -> None:
    assert connectors.is_valid_domain(domain) is valid


@pytest.mark.parametrize(
    ("host", "domain", "matches"),
    [
        ("example.com", "example.com", True),
        ("url3396.example.com", "example.com", True),
        ("WWW.Example.com", "example.com", True),
        ("notexample.com", "example.com", False),
        ("example.com.evil.net", "example.com", False),
        ("", "example.com", False),
    ],
)
def test_a_subdomain_belongs_to_its_site_and_a_lookalike_does_not(
    host: str, domain: str, matches: bool
) -> None:
    assert connectors.domain_matches(host, domain) is matches


def test_deleting_a_connector_takes_its_oauth_state(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="mcp",
        label="m",
        url="https://mcp.example/mcp",
        risk_acknowledged=True,
    )
    phase2.start_oauth(
        db,
        state="connector.abc",
        user_id=USER,
        provider="mcp",
        source_id_=None,
        connector_id_=created.id,
        code_verifier="v",
        redirect_uri="http://localhost:5173/oauth/callback",
        scopes=["mcp"],
    )
    assert connectors.delete_connector(db, user_id=USER, connector_id=created.id)
    assert phase2.consume_oauth_state(db, "connector.abc") is None
    assert connectors.delete_connector(db, user_id=USER, connector_id=created.id) is False


def test_the_state_row_names_the_connector(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="mcp",
        label="m",
        url="https://mcp.example/mcp",
        risk_acknowledged=True,
    )
    phase2.start_oauth(
        db,
        state="connector.xyz",
        user_id=USER,
        provider="mcp",
        source_id_=None,
        connector_id_=created.id,
        code_verifier="v",
        redirect_uri="http://localhost:5173/oauth/callback",
        scopes=["mcp"],
    )
    pending = phase2.consume_oauth_state(db, "connector.xyz")
    assert pending is not None and pending["connector_id"] == created.id
