"""Mint one short-lived session row from a digest — the staging deploy's job entrypoint.

    python -m motet_db.mint_session --token-sha256=<hex> --email=<address> --ttl-seconds=<n>

**The problem this solves.** Staging's ``MOTET_API_TOKEN`` is a Terraform
``random_password`` in Secret Manager, and the deploy identity deliberately holds Secret
Manager admin *minus* ``secretmanager.versions.access`` — so no agent can read it back.
Google Sign-In is not automatable either (see ``docs/testing-staging.md`` §4). The
alternative on the table was to copy the long-lived token into a shared secret store,
which documents a human step as the procedure — the exact failure mode invariant 9 warns
about. So the staging deploy mints a session instead: the workflow generates a token in
its own shell, hands *this job* nothing but the SHA-256, and returns the plaintext to the
requesting agent encrypted to a public key that agent generated.

**This module never sees, derives, or logs a plaintext token.** It takes the digest as an
argument, which is why :func:`motet_db.auth.create_session_for_digest` exists at all. A
digest is not a credential: it cannot be presented to ``/v1``, and it is one-way. That is
what makes it safe as a Cloud Run job argument, where it is recorded in the execution's
override list and readable by anyone who can read the project's job history.

**The one thing that trade gives away, and it has to be said out loud: token entropy is
now the caller's to get right, and this job cannot check it.** :func:`check_digest`
validates a *shape*. The plaintext is generated outside this repo, and the digest of it is
published in an execution record — so a token drawn from a small space would be
brute-forceable offline by anyone who can list that history, and nothing here would
notice. The requirement is at least 32 bytes from a CSPRNG, which is what
:func:`motet_db.auth.new_session_token` produces for the sign-in path and what the mint
workflow's ``openssl rand -base64 32`` produces for this one. Weakening it is not a
configuration change; it is the whole security of the credential.

**A job entrypoint, and never anything else.** Nothing in ``motet_api`` imports this
module and nothing should: there is no route behind it and no request path that reaches
it. Reachability is not what keeps it out of production, though — three independent
things would have to change for that, and only the third is in this repo:

* the job is created in staging and nowhere else,
* the mint workflow reaches staging and nothing else,
* and :data:`MINT_ENABLED_ENV`, below.

The first two are diffs a reviewer sees, and both live in the private infrastructure repo
(this one is public — see AGENTS.md's repo split). The third is this file's own interlock,
and it is the *smallest* of the three deliberately: a lock on the door of a building that
has not been built in that town.

**The record of a mint is the row, not a metric.** Nothing here is instrumented for the
obs stack, and that is a choice rather than an omission: ``auth_sessions`` already answers
"which sessions exist, for whom, minted when, expiring when" — queryably, and with a lever
attached, because each row can be revoked. A counter would be a less precise copy of a
fact already stored. The job's own log lines say what happened for the operator reading a
failed execution; they are not the durable record.

Everything else about this process is ``motet_db.migrate``: same image, same service
account, ``DATABASE_URL`` and nothing more (invariant 8 — no KMS, no vendor key, no
decrypt).
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping
from typing import Final

import psycopg

from .allowlist import ALLOWED_EMAILS_ENV, allowed_emails, is_allowed
from .auth import DIGEST_RE, AuthSession, create_session_for_digest
from .repo import OWNER_USER_ID, connect

logger = logging.getLogger(__name__)

#: The interlock. Must be exactly ``1`` in the process environment or nothing is minted.
#:
#: It is set on the staging mint job's definition and on nothing else, so a mint run
#: against any other deployment refuses before it opens a connection.
MINT_ENABLED_ENV: Final = "MOTET_STAGING_SESSION_MINT"

#: The longest session this may create — a day, against
#: :data:`motet_db.auth.DEFAULT_TTL_SECONDS`'s thirty.
#:
#: The thirty-day default is right for a human's browser and wrong for everything here: a
#: minted session exists so that one agent session can drive staging for an afternoon, and
#: an agent session does not outlive its afternoon. Capped in code as well as in the
#: workflow because the workflow's cap is an input default and this one is not negotiable
#: from outside the image.
MAX_TTL_SECONDS: Final = 24 * 60 * 60


class MintRefused(RuntimeError):
    """The mint was asked for and declined — the environment, or the arguments."""


def check_enabled(env: Mapping[str, str]) -> None:
    """Refuse unless :data:`MINT_ENABLED_ENV` is exactly ``1``.

    Exactly ``1``, not "truthy": ``0``, ``false`` and ``no`` all mean no, and a deployment
    that set the variable to something well-meant but unparsed should refuse rather than
    guess. Surrounding whitespace is stripped first, because a value that arrived through
    a YAML block or a shell heredoc is the operator's ``1`` with a newline on it. Unset
    means no, for the same reason an unset allowlist denies everybody.
    """
    if env.get(MINT_ENABLED_ENV, "").strip() != "1":
        raise MintRefused(
            f"refusing to mint a session: {MINT_ENABLED_ENV} is not set to 1. This "
            "entry point exists for the staging session mint only."
        )


def check_digest(token_sha256: str) -> str:
    """Reject anything that is not what ``sha256sum`` prints.

    Checked here as well as in :func:`motet_db.auth.create_session_for_digest` so that an
    operator reading a failed job execution is told which argument was wrong, rather than
    reading a ``ValueError`` out of the row writer.
    """
    if not DIGEST_RE.match(token_sha256):
        raise MintRefused(
            "--token-sha256 must be a 64-character lowercase hex SHA-256 digest; "
            f"got {len(token_sha256)} character(s) that do not match"
        )
    return token_sha256


def check_ttl(ttl_seconds: int) -> int:
    """Positive, and no longer than :data:`MAX_TTL_SECONDS`."""
    if ttl_seconds <= 0:
        raise MintRefused(f"--ttl-seconds must be positive; got {ttl_seconds}")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise MintRefused(
            f"--ttl-seconds must be at most {MAX_TTL_SECONDS} ({MAX_TTL_SECONDS // 3600}h); "
            f"got {ttl_seconds}"
        )
    return ttl_seconds


def check_email(email: str, env: Mapping[str, str]) -> str:
    """The address must be one the sign-in path would also accept.

    :mod:`motet_db.allowlist` is the same module ``motet_api``'s sign-in route reads, and
    reusing it rather than re-reading the variable here is the point: a mint that admitted
    an address Google sign-in would refuse would be a second, quieter door into ``/v1``.

    **An empty list and a non-matching address are the same refusal and different
    diagnoses**, so they get different messages. This job is a *different container* from
    the API service, which is where ``MOTET_ALLOWED_EMAILS`` is wired today — so "the job
    definition never injected the variable" is the likeliest way this fails on the first
    deploy, and reporting it as "that address is not on the list" would send an operator
    to read a list rather than to fix an environment. The sign-in route draws the same
    distinction, for the same reason.
    """
    allowed = allowed_emails(env)
    if not allowed:
        raise MintRefused(
            f"refusing to mint a session: {ALLOWED_EMAILS_ENV} is empty or unset in this "
            "process, so no address would be accepted. Check the job definition's "
            "environment rather than the allowlist's contents."
        )
    if not is_allowed(email, allowed):
        raise MintRefused(
            f"refusing to mint a session for {email!r}: it is not on {ALLOWED_EMAILS_ENV}, "
            "so Google sign-in would refuse the same address."
        )
    return email.strip().lower()


def mint(
    database_url: str,
    *,
    token_sha256: str,
    email: str,
    ttl_seconds: int,
    env: Mapping[str, str] | None = None,
) -> AuthSession:
    """Validate everything, then write exactly one ``auth_sessions`` row."""
    env = os.environ if env is None else env
    check_enabled(env)
    check_digest(token_sha256)
    check_ttl(ttl_seconds)
    address = check_email(email, env)

    try:
        with connect(database_url) as conn:
            session = create_session_for_digest(
                conn,
                user_id=OWNER_USER_ID,
                email=address,
                token_sha256=token_sha256,
                ttl_seconds=ttl_seconds,
            )
            conn.commit()
    except psycopg.errors.UniqueViolation as clash:
        # `auth_sessions.token_sha256` is UNIQUE, and a Cloud Run job retries a failed
        # task with the *same* arguments — so the realistic way to arrive here is a retry
        # of an execution that already committed a row and then failed on something after
        # it. Refusing loudly is the safe answer: minting a second row would be pointless
        # (the first one already works), and a raw UniqueViolation traceback would read as
        # a broken mint rather than as "this digest has already been used".
        raise MintRefused(
            "refusing to mint: a session already exists for this digest. If this is a "
            "retry, the first attempt succeeded and that token is live — revoke it with "
            "/v1/auth/logout-all and dispatch a fresh mint rather than reusing a token."
        ) from clash
    return session


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. There is deliberately no argument that carries a plaintext token."""
    parser = argparse.ArgumentParser(
        prog="motet-mint-session",
        description=(
            "Mint one short-lived Motet session from its SHA-256. Staging only; refuses "
            f"unless {MINT_ENABLED_ENV}=1."
        ),
    )
    parser.add_argument(
        "--token-sha256",
        required=True,
        help="Hex SHA-256 of the session token. The plaintext is never passed here.",
    )
    parser.add_argument("--email", required=True, help=f"An address on {ALLOWED_EMAILS_ENV}.")
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        required=True,
        help=f"How long the session lives, in seconds. At most {MAX_TTL_SECONDS}.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help=(
            "Postgres connection URL. The job must take this from $DATABASE_URL, like "
            "motet_db.migrate does: this URL carries a password, and an argument is "
            "recorded in the execution's override list where the digest is deliberately "
            "the only thing anyone can read. The flag exists for a local run."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.database_url:
        parser.error("no database URL: pass --database-url or set DATABASE_URL")

    try:
        session = mint(
            args.database_url,
            token_sha256=args.token_sha256,
            email=args.email,
            ttl_seconds=args.ttl_seconds,
        )
    except MintRefused as refusal:
        # 3 rather than 2: argparse exits 2 for a usage error, and "this deployment may
        # not mint" is a different answer from "you typed the arguments wrong".
        logger.error("%s", refusal)
        return 3

    # Session id, address and expiry — the three things an operator needs in order to
    # revoke this row or explain it later. Nothing here is a credential.
    logger.info(
        "minted session %s for %s, expires %s", session.id, session.email, session.expires_at
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
