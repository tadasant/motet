"""PROTOTYPE — connectors through the repository layer and the vault fake.

What is pinned: a site login with no password is a legal, ready row; a sealed secret
round-trips through the vault under `user_id:connector_id:kind` and fails to open under
any other row's identity; no public read returns the envelope; the site-domain uniqueness
the agent relies on; and deleting a connector takes its in-flight OAuth state with it.
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


def test_a_passwordless_site_login_is_a_complete_ready_row(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db,
        key,
        user_id=USER,
        kind="site",
        label="The Information",
        domain="theinformation.com",
        username="someone@example.com",
        secret=None,
    )
    assert created.status == "ready"
    assert created.has_secret is False
    assert connectors.load_connector_secret(db, key, connector_id=created.id) is None
    assert [c.id for c in connectors.list_connectors(db, USER)] == [created.id]


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


def test_an_mcp_token_set_seals_onto_an_existing_row(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db, key, user_id=USER, kind="mcp", label="Email", url="https://mcp.example/mcp?servers=x"
    )
    assert created.status == "needs_auth"
    tokens = json.dumps({"access_token": "at", "refresh_token": "rt"})
    stored = connectors.store_connector_secret(db, key, connector_id=created.id, secret=tokens)
    assert stored.status == "ready" and stored.has_secret
    assert (
        json.loads(connectors.load_connector_secret(db, key, connector_id=created.id) or "")[
            "refresh_token"
        ]
        == "rt"
    )


def test_no_public_read_carries_the_envelope(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db, key, user_id=USER, kind="site", label="a", domain="a.example", username="u", secret="pw"
    )
    for record in (created, connectors.get_connector(db, created.id, user_id=USER)):
        assert record is not None
        assert not any(name in vars(record) for name in ("ciphertext", "nonce", "wrapped_dek"))
        assert "pw" not in repr(record)


def test_one_site_login_per_domain(db: psycopg.Connection[Any], key: LocalKeyManager) -> None:
    connectors.create_connector(
        db, key, user_id=USER, kind="site", label="a", domain="a.example", username="u"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        connectors.create_connector(
            db, key, user_id=USER, kind="site", label="again", domain="a.example", username="v"
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("theinformation.com", "theinformation.com"),
        ("https://www.TheInformation.com/articles/x?y=1", "theinformation.com"),
        ("http://user@Host.Example:8443/", "host.example"),
        ("  www.example.com.  ", "example.com"),
    ],
)
def test_domains_normalize_to_a_bare_host(raw: str, expected: str) -> None:
    assert connectors.normalize_domain(raw) == expected


def test_deleting_a_connector_takes_its_oauth_state(
    db: psycopg.Connection[Any], key: LocalKeyManager
) -> None:
    created = connectors.create_connector(
        db, key, user_id=USER, kind="mcp", label="m", url="https://mcp.example/mcp"
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
        db, key, user_id=USER, kind="mcp", label="m", url="https://mcp.example/mcp"
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
