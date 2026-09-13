"""PROTOTYPE — the triage stage over the deterministic LLM client (invariant 7)."""

from __future__ import annotations

import json

from motet_inference import SourceItem
from motet_inference.accounting import collect_usage
from motet_inference.adapters import ClaudeTriager
from motet_inference.fakes import FakeTriager
from motet_inference.interfaces import TriageDecision
from motet_inference.llm import FakeLlmClient, LlmStage
from motet_inference.prompts import TRIAGE_TEXT_CHARS, triage_messages

PREVIEW = SourceItem(
    id="si_preview",
    title="Confusion Reigns Over White House's AI Whitelist",
    text=(
        "Two paragraphs of teaser prose about the whitelist.\n\n"
        "Read the full article: https://link.theinformation.com/click/abc123/xyz"
    ),
)
ARTICLE = SourceItem(id="si_article", title="A full newsletter", text="Body. " * 2_000)


def canned(payload: dict[str, object]) -> ClaudeTriager:
    return ClaudeTriager(FakeLlmClient(responses={"": json.dumps(payload)}))


class TestClaudeTriager:
    def test_a_teaser_is_a_fetch_with_its_url_and_domain(self) -> None:
        triager = canned(
            {
                "decision": "fetch",
                "article_url": "https://link.theinformation.com/click/abc123/xyz",
                "domain": "TheInformation.com",
                "reason": "Two paragraphs and a read-the-full-article link.",
            }
        )
        with collect_usage() as spend:
            decision = triager.triage(PREVIEW)
        assert decision.fetch
        assert decision.decision == "fetch"
        assert decision.article_url == "https://link.theinformation.com/click/abc123/xyz"
        assert decision.domain == "theinformation.com"
        assert "read-the-full-article" in decision.reason
        assert spend.requests == 1
        assert spend.entries[0].stage is LlmStage.TRIAGE

    def test_the_content_itself_is_raw(self) -> None:
        triager = canned(
            {"decision": "raw", "article_url": None, "domain": None, "reason": "It is the article."}
        )
        decision = triager.triage(ARTICLE)
        assert not decision.fetch
        assert decision.decision == "raw"
        assert decision.article_url is None

    def test_a_fetch_without_a_usable_url_degrades_to_raw(self) -> None:
        triager = canned(
            {"decision": "fetch", "article_url": "see above", "domain": None, "reason": "?"}
        )
        decision = triager.triage(PREVIEW)
        assert decision.decision == "raw"
        assert "usable URL" in decision.reason

    def test_an_unreadable_answer_degrades_to_raw_rather_than_raising(self) -> None:
        triager = ClaudeTriager(FakeLlmClient(responses={"": "not json"}))
        decision = triager.triage(PREVIEW)
        assert decision.decision == "raw"
        assert "unreadable" in decision.reason

    def test_the_prompt_carries_only_the_head_of_the_text(self) -> None:
        messages = triage_messages(ARTICLE)
        rendered = "".join(part.text for message in messages for part in message.parts)
        assert "[truncated]" in rendered
        assert len(rendered) < TRIAGE_TEXT_CHARS + 1_500
        assert ARTICLE.title in rendered

    def test_the_request_is_built_for_the_triage_stage(self) -> None:
        client = FakeLlmClient(
            responses={
                "": json.dumps(
                    {"decision": "raw", "article_url": None, "domain": None, "reason": "-"}
                )
            }
        )
        ClaudeTriager(client).triage(ARTICLE)
        request = client.calls[0]
        assert request.model == "anthropic/claude-haiku-4.5"
        assert request.reasoning is None
        assert request.response_format is not None
        assert request.response_format.name == "triage_decision"


class TestFakeTriager:
    def test_everything_is_raw_unless_scripted(self) -> None:
        scripted = TriageDecision(
            decision="fetch", article_url="https://x.example/a", domain="x.example", reason="t"
        )
        fake = FakeTriager({PREVIEW.id: scripted})
        assert fake.triage(PREVIEW) is scripted
        assert fake.triage(ARTICLE).decision == "raw"
        assert fake.calls == [PREVIEW.id, ARTICLE.id]
