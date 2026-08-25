"""The staging session mint: every refusal, and one row that actually works.

The refusals are the interesting half. This entry point writes an ``auth_sessions`` row
without anybody signing in, so what keeps it honest is the set of things it declines to
do — and each of those is a one-line change away from not being declined, which is exactly
what a test is for.

The happy path is asserted through :func:`motet_db.auth.session_for_token`, not by reading
the row back: the claim worth making is *"a token whose digest was minted here resolves on
the ordinary session path"*, and only the ordinary lookup can support it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from motet_db import auth, mint_session
from motet_db.allowlist import ALLOWED_EMAILS_ENV
from motet_db.mint_session import MINT_ENABLED_ENV, MintRefused

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_postgres = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not set")

ALLOWED = "owner@motet.test"
#: A digest, not a token: 64 lowercase hex characters, which is all this entry point ever
#: handles. Computed rather than written out so that the shape comes from the real hasher.
DIGEST = auth.token_digest("only-ever-hashed-inside-this-test")

#: A database URL that is never connected to. Every refusal below must happen before the
#: connection is opened, so a test that reaches Postgres with this value fails loudly
#: rather than silently proving nothing.
UNREACHABLE_DB = "postgresql://nobody@127.0.0.1:1/never"

#: The runpy warning the entry-point guards below exist to keep out of a job's logs.
DOUBLE_EXECUTION = "found in sys.modules after import of package"

#: Every subprocess here answers in well under a second; the bound is so that one which
#: somehow blocks fails in a minute rather than hanging the job.
TIMEOUT_SECONDS = 60


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment the staging mint job runs in: interlock on, allowlist configured."""
    monkeypatch.setenv(MINT_ENABLED_ENV, "1")
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, ALLOWED)


class TestTheInterlock:
    """`MOTET_STAGING_SESSION_MINT=1`, or nothing happens."""

    def test_an_unset_variable_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MINT_ENABLED_ENV, raising=False)
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, ALLOWED)
        with pytest.raises(MintRefused, match=MINT_ENABLED_ENV):
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)

    @pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", "1 1", "01"])
    def test_only_the_literal_1_enables_it(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Truthiness is not a security boundary; the literal is."""
        monkeypatch.setenv(MINT_ENABLED_ENV, value)
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, ALLOWED)
        with pytest.raises(MintRefused, match=MINT_ENABLED_ENV):
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)

    def test_the_interlock_is_checked_before_anything_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disabled deployment refuses on the interlock, not on the arguments.

        Otherwise a badly formed argument would mask the fact that this deployment was
        never allowed to mint at all — the more important of the two answers.
        """
        monkeypatch.delenv(MINT_ENABLED_ENV, raising=False)
        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        with pytest.raises(MintRefused, match=MINT_ENABLED_ENV):
            mint_session.mint(
                UNREACHABLE_DB,
                token_sha256="not-a-digest",
                email="who@example.invalid",
                ttl_seconds=-1,
            )

    def test_main_exits_non_zero_when_it_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(MINT_ENABLED_ENV, raising=False)
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, ALLOWED)
        code = mint_session.main(
            [
                f"--token-sha256={DIGEST}",
                f"--email={ALLOWED}",
                "--ttl-seconds=3600",
                f"--database-url={UNREACHABLE_DB}",
            ]
        )
        assert code != 0


class TestTheAllowlist:
    """The same list the sign-in route reads, asked the same question."""

    def test_an_address_not_on_the_list_is_refused(self, enabled: None) -> None:
        with pytest.raises(MintRefused, match=ALLOWED_EMAILS_ENV):
            mint_session.mint(
                UNREACHABLE_DB,
                token_sha256=DIGEST,
                email="somebody-else@example.invalid",
                ttl_seconds=3600,
            )

    def test_an_unset_allowlist_denies_rather_than_allows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MINT_ENABLED_ENV, "1")
        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        with pytest.raises(MintRefused, match=ALLOWED_EMAILS_ENV):
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)


class TestTheArguments:
    def test_an_uppercase_digest_is_refused(self, enabled: None) -> None:
        """`sha256sum` prints lowercase, and a row stored otherwise never matches."""
        with pytest.raises(MintRefused, match="token-sha256"):
            mint_session.mint(
                UNREACHABLE_DB, token_sha256=DIGEST.upper(), email=ALLOWED, ttl_seconds=3600
            )

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "deadbeef",
            DIGEST[:-1],
            DIGEST + "a",
            DIGEST[:-1] + "z",
            f"{DIGEST}\n",
            f" {DIGEST}",
        ],
    )
    def test_a_malformed_digest_is_refused(self, value: str, enabled: None) -> None:
        with pytest.raises(MintRefused, match="token-sha256"):
            mint_session.mint(UNREACHABLE_DB, token_sha256=value, email=ALLOWED, ttl_seconds=3600)

    @pytest.mark.parametrize("value", [0, -1, -3600])
    def test_a_non_positive_ttl_is_refused(self, value: int, enabled: None) -> None:
        with pytest.raises(MintRefused, match="ttl-seconds"):
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=value)

    def test_a_ttl_beyond_the_cap_is_refused(self, enabled: None) -> None:
        with pytest.raises(MintRefused, match="ttl-seconds"):
            mint_session.mint(
                UNREACHABLE_DB,
                token_sha256=DIGEST,
                email=ALLOWED,
                ttl_seconds=mint_session.MAX_TTL_SECONDS + 1,
            )

    def test_the_cap_is_far_below_the_browser_default(self) -> None:
        """A minted session is an afternoon's credential, not a month's."""
        assert mint_session.MAX_TTL_SECONDS == 24 * 60 * 60
        assert mint_session.MAX_TTL_SECONDS < auth.DEFAULT_TTL_SECONDS

    def test_missing_database_url_is_an_argument_error(
        self, enabled: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as exit_info:
            mint_session.main(
                [f"--token-sha256={DIGEST}", f"--email={ALLOWED}", "--ttl-seconds=3600"]
            )
        assert exit_info.value.code != 0


class TestThePlaintextNeverGetsHere:
    """The property the whole split exists for, asserted rather than assumed."""

    def test_the_only_token_argument_is_the_digest(self) -> None:
        flags = set(re.findall(r"--token[a-z0-9-]*", mint_session.build_parser().format_help()))
        assert flags == {"--token-sha256"}

    def test_the_module_cannot_derive_a_token_or_hash_one(self) -> None:
        """It imports neither the generator nor the hasher, so it has nothing to leak.

        A source-level assertion because that is the level the claim lives at: were this
        module to call `token_digest`, it would be taking plaintext from *somewhere*, and
        the somewhere would be a Cloud Run job argument — recorded on the execution and
        readable by anyone who can list the project's job history.
        """
        source = Path(mint_session.__file__).read_text()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "new_session_token" not in code
        assert "token_digest" not in code
        assert "create_session(" not in code


class TestTheEntryPointRunsOnce:
    """`python -m motet_db.mint_session` must execute this module exactly once.

    The rule `motet_workers.runner` learned the hard way (motet#21): importing the package
    first pulls `__init__` in, and anything `__init__` re-exported from the entry point is
    then executed a *second* time under a second name, with a second copy of every
    module-level object. `runpy` says so on stderr and carries on to exit 0, which is how
    it reached a production image. Both checks run in a fresh interpreter, because this
    one has already imported everything.

    See `workers/tests/test_entrypoint.py`, which is the same guard on the other entry
    point and explains the `-W error::RuntimeWarning` idiom in full.
    """

    def test_importing_the_package_does_not_import_the_entry_point(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, motet_db; sys.exit('motet_db.mint_session' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, (
            "importing `motet_db` pulled `motet_db.mint_session` into sys.modules, so "
            "`python -m motet_db.mint_session` now runs it twice. Take the import back "
            "out of `motet_db/__init__.py`. See motet#21."
        )

    def test_the_entry_point_prints_help_without_a_runtime_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "-m", "motet_db.mint_session", "-h"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        assert DOUBLE_EXECUTION not in result.stderr, result.stderr
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("usage: motet-mint-session"), result.stdout

    def test_the_entry_point_refuses_from_a_shell_with_the_interlock_unset(self) -> None:
        """The refusal an operator would actually see, from a real process.

        Nothing else in this file runs the module as `__main__`, and "exits non-zero when
        it is not allowed to mint" is the one claim worth proving at that level.
        """
        env = {k: v for k, v in os.environ.items() if k != MINT_ENABLED_ENV}
        env[ALLOWED_EMAILS_ENV] = ALLOWED
        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::RuntimeWarning",
                "-m",
                "motet_db.mint_session",
                f"--token-sha256={DIGEST}",
                f"--email={ALLOWED}",
                "--ttl-seconds=3600",
                f"--database-url={UNREACHABLE_DB}",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=TIMEOUT_SECONDS,
        )
        assert DOUBLE_EXECUTION not in result.stderr, result.stderr
        assert result.returncode != 0
        assert MINT_ENABLED_ENV in result.stdout + result.stderr


@needs_postgres
class TestTheRowItWrites:
    def test_a_minted_session_resolves_on_the_ordinary_lookup(
        self, db: psycopg.Connection[Any], enabled: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: a workflow's shell hashes a token, this mints, the token works.

        `token` here stands in for the value the mint workflow generates and encrypts to
        the requesting agent. Only its digest crosses into the mint, which is the whole
        arrangement in one assertion.
        """
        assert DATABASE_URL is not None
        token = auth.new_session_token()

        session = mint_session.mint(
            DATABASE_URL,
            token_sha256=auth.token_digest(token),
            email=ALLOWED,
            ttl_seconds=8 * 3600,
        )

        assert session.user_id == "motet-owner"
        assert session.email == ALLOWED

        resolved = auth.session_for_token(db, token)
        assert resolved is not None
        assert resolved.id == session.id

    def test_the_expiry_is_the_ttl_it_was_given(
        self, db: psycopg.Connection[Any], enabled: None
    ) -> None:
        assert DATABASE_URL is not None
        session = mint_session.mint(
            DATABASE_URL, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=8 * 3600
        )
        expected = datetime.now(UTC) + timedelta(hours=8)
        assert abs((session.expires_at - expected).total_seconds()) < 60

    def test_the_address_is_stored_as_the_allowlist_reads_it(
        self, db: psycopg.Connection[Any], enabled: None
    ) -> None:
        """Lowercased, like the comparison — so the session's `email` is the listed one."""
        assert DATABASE_URL is not None
        session = mint_session.mint(
            DATABASE_URL, token_sha256=DIGEST, email=f"  {ALLOWED.upper()} ", ttl_seconds=3600
        )
        assert session.email == ALLOWED

    def test_main_mints_and_logs_no_credential(
        self,
        db: psycopg.Connection[Any],
        enabled: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert DATABASE_URL is not None
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        token = auth.new_session_token()

        with caplog.at_level("INFO"):
            code = mint_session.main(
                [
                    f"--token-sha256={auth.token_digest(token)}",
                    f"--email={ALLOWED}",
                    "--ttl-seconds=3600",
                ]
            )

        assert code == 0
        assert auth.session_for_token(db, token) is not None
        assert token not in caplog.text
