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

import ast
import os
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
        with pytest.raises(MintRefused, match="is not on"):
            mint_session.mint(
                UNREACHABLE_DB,
                token_sha256=DIGEST,
                email="somebody-else@example.invalid",
                ttl_seconds=3600,
            )

    @pytest.mark.parametrize("value", [None, "", "   ", ",", " , "])
    def test_an_unset_allowlist_denies_rather_than_allows(
        self, value: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MINT_ENABLED_ENV, "1")
        if value is None:
            monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        else:
            monkeypatch.setenv(ALLOWED_EMAILS_ENV, value)
        with pytest.raises(MintRefused, match="empty or unset"):
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)

    def test_the_two_allowlist_refusals_say_different_things(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same refusal, different diagnosis — and this job is where they diverge.

        The mint job is a different container from the API service, so "the job definition
        never injected `MOTET_ALLOWED_EMAILS`" is the likeliest first-deploy failure. If it
        reported as "that address is not on the list", an operator would go and read a list
        that is not the problem.
        """
        monkeypatch.setenv(MINT_ENABLED_ENV, "1")

        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
        with pytest.raises(MintRefused) as unset:
            mint_session.mint(UNREACHABLE_DB, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=60)

        monkeypatch.setenv(ALLOWED_EMAILS_ENV, ALLOWED)
        with pytest.raises(MintRefused) as wrong:
            mint_session.mint(
                UNREACHABLE_DB, token_sha256=DIGEST, email="nope@example.invalid", ttl_seconds=60
            )

        assert "job definition" in str(unset.value)
        assert "job definition" not in str(wrong.value)
        assert "nope@example.invalid" in str(wrong.value)


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

    def test_the_cli_surface_is_exactly_these_four_arguments(self) -> None:
        """The whole option set, not just the ones starting with `--token`.

        Asserted as an equality so that *any* new argument fails this test and has to be
        justified here — `--secret`, `--password` or a bare positional would all sail past
        a pattern that only looked for token-shaped names.
        """
        flags = {
            option
            for action in mint_session.build_parser()._actions
            for option in action.option_strings
        }
        assert flags == {
            "-h",
            "--help",
            "--token-sha256",
            "--email",
            "--ttl-seconds",
            "--database-url",
        }

    def test_the_module_cannot_derive_a_token_or_hash_one(self) -> None:
        """It imports neither the generator nor a hasher, so it has nothing to leak.

        Walked as an AST rather than grepped, so that a docstring *mentioning*
        `token_digest` does not fail and an inline `hashlib.sha256(...)` does not pass. The
        claim lives at this level: were this module to hash anything, it would be taking a
        plaintext from *somewhere* — and the somewhere would be a Cloud Run job argument,
        recorded on the execution and readable by anyone who can list the project's job
        history.
        """
        tree = ast.parse(Path(mint_session.__file__).read_text())
        imported = {
            alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert "hashlib" not in imported
        assert not imported & {"new_session_token", "token_digest", "create_session"}

        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
        }
        assert not called & {"new_session_token", "token_digest", "create_session", "sha256"}


class TestTheEntryPointRefusesFromAShell:
    """The mint-specific half of the entry-point guards.

    The generic ones — that importing `motet_db` does not pull this module into
    `sys.modules`, that `--help` is clean of runpy's double-execution warning on *stderr*,
    and that the usage line names something typeable — live in `db/tests/test_db_entrypoints.py`
    and run against every `python -m motet_db.*` entry point rather than only this one. See
    motet#21 and motet#27. What is here is the one claim that is about *this* job.
    """

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
        self, db: psycopg.Connection[Any], enabled: None
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

    def test_main_mints_from_a_command_line_and_logs_nothing_secret(
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
        # Cheap insurance rather than a load-bearing assertion: `main` is never handed the
        # plaintext, so this cannot fail today. It fails the day somebody adds an argument
        # that carries one — which is exactly when a log line would start leaking it.
        assert token not in caplog.text

    def test_minting_the_same_digest_twice_is_a_refusal_not_a_traceback(
        self, db: psycopg.Connection[Any], enabled: None
    ) -> None:
        """`auth_sessions.token_sha256` is UNIQUE, and a failed Cloud Run task is retried.

        A retry arrives with the *same* arguments, so the second insert must produce a
        message an operator can act on rather than a `UniqueViolation` traceback that reads
        as a broken mint — when in fact the first attempt committed a live session.
        """
        assert DATABASE_URL is not None
        mint_session.mint(DATABASE_URL, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)

        with pytest.raises(MintRefused, match="already exists for this digest"):
            mint_session.mint(DATABASE_URL, token_sha256=DIGEST, email=ALLOWED, ttl_seconds=3600)

        counted = db.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()
        assert counted is not None and counted["n"] == 1
