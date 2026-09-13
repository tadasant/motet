"""Redaction, which is the one thing in this service that must not be wrong quietly.

What gets stored is a transcript of an agent that held the owner's mailbox token, drove a
browser through a login, and read pages written by strangers. The two rules
:mod:`motet_enrich.redact` implements are tested separately here because they fail
differently: rule 1 (a non-browser tool's result is never stored) is a rule about
provenance and either holds or does not, while rule 2 (the patterns) is a backstop whose
coverage is a judgement — so the tests for it are about the shapes actually seen in the
spike rather than about completeness it does not claim.
"""

from __future__ import annotations

from motet_enrich.redact import (
    MAX_KEPT_CHARS,
    REDACTED,
    Redactor,
    is_browser_tool,
    summarize_foreign_result,
)


class TestRuleOneProvenance:
    """A result is kept because of *which server* answered, never because of how it looks."""

    def test_the_browser_servers_tools_are_the_only_ones_kept(self) -> None:
        assert is_browser_tool("browser__browser_execute")
        assert not is_browser_tool("cn-9f2a__search_email_conversations")
        assert not is_browser_tool(None)
        assert not is_browser_tool("")

    def test_an_un_namespaced_name_is_not_the_browser_server(self) -> None:
        """The namespace is the only part of a tool name this process controls.

        Accepting a bare ``browser_*`` prefix handed result retention to any connected MCP
        server that exposed a tool called ``browser_search`` — and the owner's mailbox
        server is exactly the one where that matters.
        """
        assert not is_browser_tool("browser_execute")
        assert not is_browser_tool("browser__")
        assert not is_browser_tool("__browser_execute")

    def test_a_connector_cannot_name_itself_into_retention(self) -> None:
        """A user-supplied server called ``browserish`` is not the browser server.

        The namespace is compared whole rather than as a prefix, because the slug a
        connector gets is derived from its id — and this is the assertion that keeps that
        derivation load-bearing.
        """
        assert not is_browser_tool("browserish__browser_execute")
        assert not is_browser_tool("mybrowser__anything")

    def test_a_foreign_result_is_replaced_by_its_size(self) -> None:
        note = summarize_foreign_result("cn-9f2a__get_email", "Your sign-in link: https://x/y")
        assert "sign-in" not in note
        assert "30 chars from cn-9f2a__get_email" in note
        assert summarize_foreign_result("t", None) == "<0 chars from t, not stored>"


class TestRuleTwoThePatterns:
    def test_the_runs_own_secrets_go_by_exact_match(self) -> None:
        redact = Redactor(["hunter2-the-password", "owner@example.com", "tok_abcdef123456"])
        cleaned = redact("filled hunter2-the-password for owner@example.com with tok_abcdef123456")
        assert "hunter2" not in cleaned
        assert "owner@example.com" not in cleaned
        assert "tok_abcdef123456" not in cleaned

    def test_a_short_value_is_not_treated_as_a_secret(self) -> None:
        """Redacting a three-character string would redact every occurrence of it.

        A site whose username is ``abc`` would otherwise turn every ``abc`` in every page
        excerpt into ``<redacted>``, which makes the transcript unreadable and protects
        nothing that is not already guessable.
        """
        assert Redactor(["abc"])("abc def abc") == "abc def abc"

    def test_a_bearer_header_is_removed_even_when_nobody_named_it(self) -> None:
        cleaned = Redactor()("Authorization: Bearer ya29.a0AfB_byC-longtokenvalue")
        assert "ya29" not in cleaned
        assert f"Bearer {REDACTED}" in cleaned

    def test_a_credential_carrying_query_value_is_removed(self) -> None:
        cleaned = Redactor()(
            "page.goto('https://example.com/articles/x?eu=Zm9vYmFyYmF6&utm_source=newsletter')"
        )
        assert "Zm9vYmFyYmF6" not in cleaned
        assert "eu=<redacted>" in cleaned
        # The parameters that are not credentials survive, because a transcript nobody can
        # read is a transcript nobody looks at.
        assert "utm_source=newsletter" in cleaned

    def test_a_magic_links_opaque_segment_goes_and_an_article_slug_stays(self) -> None:
        cleaned = Redactor()(
            "https://example.com/sessions/confirm/QUJDREVGR0hJSktMTU5PUFFSU1RVVldY then "
            "https://example.com/articles/openai-raises-again-at-a-higher-valuation"
        )
        assert "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY" not in cleaned
        assert "openai-raises-again-at-a-higher-valuation" in cleaned

    def test_a_cookie_value_goes_even_when_the_run_never_saw_the_cookie(self) -> None:
        cleaned = Redactor()('{"name":"session","value":"s%3AabcdefghijklmnopQR"}')
        assert "abcdefghijkl" not in cleaned
        assert '"name":"session"' in cleaned

    def test_an_address_nobody_declared_is_still_removed(self) -> None:
        assert "someone@elsewhere.test" not in str(Redactor()("write to someone@elsewhere.test"))


class TestClipping:
    def test_a_long_result_is_redacted_first_and_bounded_second(self) -> None:
        """Both, in that order, or neither claim is true.

        Truncating first would leave a secret that happened to sit past the cut *in* the
        stored string, because the pattern pass would never see it.
        """
        redact = Redactor(["supersecretvalue"])
        text = ("x" * MAX_KEPT_CHARS) + " supersecretvalue"
        clipped = redact.clip(text)
        assert clipped is not None
        assert "supersecretvalue" not in clipped
        assert clipped.endswith("chars)")
        assert len(clipped) < len(text) + 40

    def test_none_survives_as_none(self) -> None:
        assert Redactor().clip(None) is None
        assert Redactor()(None) is None
