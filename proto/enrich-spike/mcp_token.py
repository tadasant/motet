"""Print the email connector's current MCP access token to stdout — nothing else, ever.

Spike helper. Opens the sealed token set on the `connectors` row exactly the way a worker
would (KeyManager → open_sealed), refreshes it through the token endpoint recorded on the
row when it is within a minute of expiry, re-seals the new set, and prints the access
token alone. Meant to be called by pi-mcp-adapter's `!command` header hook at connect time:

    UV_ENV_FILE=.env uv run python proto/enrich-spike/mcp_token.py [connector_id] [--refresh]

Requires GOOGLE_APPLICATION_CREDENTIALS (the local-dev service-account key) for the kms
vault backend. Diagnostics go to stderr and never include a token.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from motet_api.mcp_oauth import HttpMcpOAuthClient, TokenSet
from motet_db.connectors import get_connector, load_connector_secret, store_connector_secret
from motet_db.repo import connect
from motet_vault.envelope import build_key_manager

DEFAULT_CONNECTOR = "cn_e1bd093da927"
REFRESH_MARGIN = timedelta(seconds=60)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--refresh"]
    force = "--refresh" in sys.argv[1:]
    connector_id = args[0] if args else DEFAULT_CONNECTOR
    manager = build_key_manager()
    with connect(os.environ["DATABASE_URL"]) as conn:
        row = get_connector(conn, connector_id, user_id=_owner(conn))
        if row is None:
            print(f"connector {connector_id} not found", file=sys.stderr)
            return 2
        raw = load_connector_secret(conn, manager, connector_id=connector_id)
        if raw is None:
            print(f"connector {connector_id} holds no secret", file=sys.stderr)
            return 2
        doc = json.loads(raw)
        expires_at = datetime.fromisoformat(doc["expires_at"]) if doc.get("expires_at") else None
        now = datetime.now(UTC)
        if force or (expires_at is not None and expires_at - REFRESH_MARGIN <= now):
            if not doc.get("refresh_token"):
                print("access token expired and no refresh token", file=sys.stderr)
                return 3
            if not (row.oauth_token_endpoint and row.oauth_client_id and row.oauth_resource):
                print("connector row lacks token endpoint / client id / resource", file=sys.stderr)
                return 3
            fresh: TokenSet = HttpMcpOAuthClient().refresh(
                token_endpoint=row.oauth_token_endpoint,
                client_id=row.oauth_client_id,
                refresh_token=doc["refresh_token"],
                resource=row.oauth_resource,
            )
            if fresh.refresh_token is None:  # a server that rotates nothing keeps the old one
                fresh = TokenSet(
                    access_token=fresh.access_token,
                    refresh_token=doc["refresh_token"],
                    expires_at=fresh.expires_at,
                    token_type=fresh.token_type,
                    scope=fresh.scope,
                )
            store_connector_secret(
                conn,
                manager,
                connector_id=connector_id,
                secret=fresh.to_json(),
                expires_at=fresh.expires_at,
            )
            conn.commit()
            expiry = fresh.expires_at.isoformat() if fresh.expires_at else "none"
            print(f"refreshed; new expiry {expiry}", file=sys.stderr)
            doc = json.loads(fresh.to_json())
        else:
            print(
                f"token valid; expires {expires_at.isoformat() if expires_at else 'never'}",
                file=sys.stderr,
            )
    sys.stdout.write(doc["access_token"])
    return 0


def _owner(conn) -> str:  # type: ignore[no-untyped-def]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users ORDER BY created_at LIMIT 1")
        row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


if __name__ == "__main__":
    sys.exit(main())
