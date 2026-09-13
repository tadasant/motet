"""The real runner's *configuration*, which is the half a fake cannot cover.

No test here starts a browser, reaches OpenRouter, or spends a cent — invariant 7, and this
package's whole seam. What it does instead is the argument ``api/tests/test_drain.py``
makes about the Cloud Run adapter: the interesting failures in a subprocess runner are not
in the subprocess, they are in the files and the argv and the environment handed to it, and
every one of those ships green and fails at the vendor.

So the toolchain is a directory of empty files, ``PiRunner`` is asked to write its layout,
and the layout is read back. The stream parser is driven over the JSON lines Pi's own
``docs/json.md`` documents.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from motet_enrich.config import EnrichSettings, load_settings
from motet_enrich.contract import EnrichRequest, McpServer, RunCaps, SiteCredential
from motet_enrich.pi import (
    BROWSER_SERVER_NAME,
    SITE_PASSWORD_ENV,
    PiRunner,
    _allowed_hosts,
    _assistant_text,
    _Collected,
    _host_of,
    _parse_line,
    _redacted,
)
from motet_enrich.prompt import build_prompt, parse_answer
from motet_enrich.redact import BROWSER_SERVER, Redactor
from motet_enrich.runner import FakeRunner


@pytest.fixture
def toolchain(tmp_path: Path) -> Path:
    root = tmp_path / "toolchain"
    (root / "node_modules" / ".bin").mkdir(parents=True)
    for name in ("pi-mcp-adapter", "playwright-stealth-mcp-server"):
        (root / "node_modules" / name).mkdir()
    pi = root / "node_modules" / ".bin" / "pi"
    pi.write_text("#!/bin/sh\nexit 0\n")
    pi.chmod(0o755)
    (root / "harness").mkdir()
    (root / "harness" / "browser-mcp.mjs").write_text("// harness\n")
    (tmp_path / "browsers" / "chromium-1").mkdir(parents=True)
    return root


@pytest.fixture
def settings(toolchain: Path, tmp_path: Path) -> EnrichSettings:
    return load_settings(
        {
            "MOTET_INFERENCE_MODE": "real",
            "OPENROUTER_API_KEY": "sk-or-test",
            "MOTET_ENRICH_TOOLCHAIN_DIR": str(toolchain),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "browsers"),
        }
    )


def a_request(**overrides: Any) -> EnrichRequest:
    body: dict[str, Any] = {
        "item_id": "si_1",
        "title": "Today",
        "preview_text": "preview",
        "candidate_urls": ["https://url1.example.com/ls/click?upn=abc"],
        "site": SiteCredential(domain="example.com", username="owner@example.com"),
        "caps": RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=600),
    }
    body.update(overrides)
    return EnrichRequest(**body)


class TestTheLayoutOneRunGets:
    def test_the_mcp_document_names_the_browser_and_every_server(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        runner = PiRunner(settings)
        workdir = tmp_path / "run"
        workdir.mkdir()
        request = a_request(
            mcp_servers=[McpServer(name="cn-1", url="https://mail.example/mcp", access_token="t0k")]
        )
        runner._write_layout(workdir, request)

        document = json.loads((workdir / ".pi" / "mcp.json").read_text())
        assert set(document["mcpServers"]) == {BROWSER_SERVER_NAME, "cn-1"}
        assert document["directTools"] is True

    def test_no_bearer_is_written_into_the_mcp_document(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        """The adapter runs a ``!command`` for a header value at connect time.

        So what is on disk in a config a subprocess reads is a path, and the token itself
        is in a 0600 file this process wrote. A token inlined here would be in a file the
        agent's own toolchain can read with no tool call at all.
        """
        runner = PiRunner(settings)
        workdir = tmp_path / "run"
        workdir.mkdir()
        runner._write_layout(
            workdir,
            a_request(
                mcp_servers=[
                    McpServer(name="cn-1", url="https://mail.example/mcp", access_token="t0ken-xyz")
                ]
            ),
        )
        document = (workdir / ".pi" / "mcp.json").read_text()
        assert "t0ken-xyz" not in document
        assert document.count("!") >= 1
        assert (workdir / "bearer-cn-1").read_text().strip() == "Bearer t0ken-xyz"

    def test_the_bearer_file_is_not_world_readable(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        runner = PiRunner(settings)
        workdir = tmp_path / "run"
        workdir.mkdir()
        runner._write_layout(
            workdir,
            a_request(
                mcp_servers=[McpServer(name="cn-1", url="https://m.example/mcp", access_token="t")]
            ),
        )
        assert oct(os.stat(workdir / "bearer-cn-1").st_mode)[-3:] == "600"
        assert oct(os.stat(workdir / "bearer-cn-1.sh").st_mode)[-3:] == "700"

    def test_the_model_row_is_priced_from_the_shared_catalogue(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        """Never a second hand-written price: that is a second number to be wrong."""
        from motet_inference.llm import KNOWN_MODELS

        runner = PiRunner(settings)
        workdir = tmp_path / "run"
        workdir.mkdir()
        runner._write_layout(workdir, a_request())
        document = json.loads((workdir / "agent" / "models.json").read_text())
        provider = next(iter(document["providers"].values()))
        row = provider["models"][0]
        spec = KNOWN_MODELS[settings.model]
        assert row["id"] == spec.slug
        assert row["cost"]["input"] == spec.input_usd_per_mtok
        assert row["cost"]["cacheRead"] == spec.cache_read_usd_per_mtok
        # The key is interpolated by pi from the child environment, never written here.
        assert provider["apiKey"] == "$OPENROUTER_API_KEY"
        assert "sk-or-test" not in (workdir / "agent" / "models.json").read_text()


class TestTheBrowserServersEnvironment:
    """The one control in this file that is a control rather than hygiene.

    ``browser_execute`` evaluates the model's JavaScript in the browser server's own Node
    process, so anything in that process's environment is readable by whatever the model
    was talked into writing — with third-party page text in its context.
    """

    def test_it_carries_no_vendor_key_and_no_service_token(
        self, settings: EnrichSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MOTET_ENRICH_SERVICE_TOKEN", "sharedsecret")
        runner = PiRunner(settings)
        env = runner._browser_env(tmp_path, a_request())
        assert "OPENROUTER_API_KEY" not in env
        assert "MOTET_ENRICH_SERVICE_TOKEN" not in env
        assert "sk-or-test" not in json.dumps(env)

    def test_the_mcp_document_refuses_to_let_the_browser_inherit_this_process(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        document = PiRunner(settings)._mcp_document(tmp_path, a_request())
        assert document["mcpServers"][BROWSER_SERVER_NAME]["inheritEnv"] is False

    def test_the_site_password_is_there_and_is_in_no_prompt(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        request = a_request(
            site=SiteCredential(domain="example.com", username="o@e.com", password="hunter2")
        )
        env = PiRunner(settings)._browser_env(tmp_path, request)
        assert env[SITE_PASSWORD_ENV] == "hunter2"
        assert "hunter2" not in build_prompt(request)

    def test_certificate_validation_stays_on(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        """The published server defaults it *off* for container convenience.

        On the open internet that is the difference between a paywall and anyone on the
        path reading the owner's session.
        """
        assert (
            PiRunner(settings)._browser_env(tmp_path, a_request())["IGNORE_HTTPS_ERRORS"] == "false"
        )

    def test_the_allowed_hosts_are_the_site_and_the_links_and_nothing_else(
        self, settings: EnrichSettings, tmp_path: Path
    ) -> None:
        request = a_request(
            candidate_urls=[
                "https://url1.example.com/a",
                "https://links.sender.test/ls/click?u=1",
            ]
        )
        hosts = PiRunner(settings)._browser_env(tmp_path, request)["MOTET_ALLOWED_HOSTS"]
        assert set(hosts.split(",")) == {"example.com", "url1.example.com", "links.sender.test"}


class TestTheArgv:
    def test_the_agent_gets_no_builtin_tools_and_no_project_context(
        self, settings: EnrichSettings
    ) -> None:
        """A file-editing tool in a container holding a live session is capability nothing
        in this task needs, and a stray AGENTS.md in the image is instructions nobody wrote
        for this agent."""
        argv = PiRunner(settings)._argv(a_request())
        for flag in ("--no-builtin-tools", "--no-session", "--no-context-files", "--no-skills"):
            assert flag in argv

    def test_the_model_carries_the_thinking_level(self, settings: EnrichSettings) -> None:
        argv = PiRunner(settings)._argv(a_request())
        assert argv[argv.index("--model") + 1] == f"{settings.model}:low"

    def test_the_prompt_is_the_last_argument_after_a_double_dash(
        self, settings: EnrichSettings
    ) -> None:
        argv = PiRunner(settings)._argv(a_request())
        assert argv[-2] == "--"
        assert "Candidate links" in argv[-1]


class TestTheEventStream:
    def test_a_tool_call_and_its_result_become_transcript_entries(self) -> None:
        collected = _Collected()
        for line in (
            '{"type":"agent_start"}',
            '{"type":"tool_execution_start","toolCallId":"1","toolName":"browser__browser_execute",'
            '"args":{"code":"page.goto(\'https://example.com\')"}}',
            '{"type":"tool_execution_end","toolCallId":"1","toolName":"browser__browser_execute",'
            '"result":{"success":true},"isError":false}',
        ):
            parsed = _parse_line(line)
            assert parsed is not None
            collected.absorb(parsed)
        assert collected.tool_calls == 1
        assert [entry.kind for entry in collected.entries] == ["tool_call", "tool_result"]
        assert collected.entries[1].ok is True

    def test_the_cumulative_cost_is_read_off_usage(self) -> None:
        collected = _Collected()
        for total in (0.01, 0.04, 0.04):
            parsed = _parse_line(
                json.dumps({"type": "message_update", "usage": {"cost": {"total": total}}})
            )
            assert parsed is not None
            collected.absorb(parsed)
        assert collected.cost_usd == pytest.approx(0.04)

    def test_the_final_assistant_message_is_the_answer(self) -> None:
        collected = _Collected()
        message = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "STATUS: ok\nLOGGED_IN: no"}],
            },
        }
        parsed = _parse_line(json.dumps(message))
        assert parsed is not None
        collected.absorb(parsed)
        assert collected.final_text is not None
        assert collected.final_text.startswith("STATUS: ok")

    def test_a_line_that_is_not_ours_is_ignored_rather_than_fatal(self) -> None:
        """A dependency that writes to stdout costs one transcript entry, not the article."""
        assert _parse_line("npm warn deprecated something\n") is None
        assert _parse_line("{not json}") is None
        assert _parse_line("[1,2,3]") is None

    def test_a_user_message_is_not_read_as_the_agents_answer(self) -> None:
        assert _assistant_text({"role": "user", "content": "hello"}) is None
        assert _assistant_text(None) is None


class TestRedactionAtTheBoundary:
    def test_a_non_browser_result_is_replaced_and_a_browser_one_is_cleaned(self) -> None:
        from motet_enrich.contract import TranscriptEntry

        entries = [
            TranscriptEntry(
                seq=1,
                kind="tool_result",
                tool="cn-1__get_email",
                ok=True,
                result="Hi owner@example.com, your link: https://example.com/s/AAAAAAAAAAAAAAAAAAAAAAAAAA",
            ),
            TranscriptEntry(
                seq=2,
                kind="tool_result",
                tool=f"{BROWSER_SERVER}__browser_execute",
                ok=True,
                result="opened https://example.com/articles/x?eu=SECRETTOKEN",
            ),
        ]
        cleaned = list(_redacted(entries, Redactor(["owner@example.com"])))
        assert cleaned[0].result is not None
        assert "owner@example.com" not in cleaned[0].result
        assert "not stored" in cleaned[0].result
        assert cleaned[1].result is not None
        assert "SECRETTOKEN" not in cleaned[1].result
        assert "articles/x" in cleaned[1].result


class TestParsingTheAnswer:
    def test_an_ok_answer_with_an_article(self) -> None:
        status, login, article, url = parse_answer(
            "STATUS: ok\nLOGGED_IN: yes\nARTICLE_URL: https://example.com/articles/x\n\n"
            "```ARTICLE_MARKDOWN\n# Head\n\nBody.\n```"
        )
        assert (status, login, url) == ("ok", True, "https://example.com/articles/x")
        assert article == "# Head\n\nBody."

    def test_an_ok_answer_with_no_article_is_not_ok(self) -> None:
        """The caller has nothing to store, and reporting a success would put an empty
        article over a perfectly good newsletter preview."""
        assert parse_answer("STATUS: ok\nLOGGED_IN: no\n")[0] == "blocked"

    def test_a_blocked_answer(self) -> None:
        answer = parse_answer("STATUS: blocked\nLOGGED_IN: no\nOnly social sign-in.")
        assert answer == ("blocked", False, None, None)

    def test_an_answer_in_no_shape_at_all_is_blocked(self) -> None:
        assert parse_answer("I could not do it, sorry.") == ("blocked", False, None, None)

    def test_not_needed_is_not_a_login(self) -> None:
        assert parse_answer("STATUS: blocked\nLOGGED_IN: not-needed")[1] is False


class TestHostParsing:
    def test_a_host_is_taken_off_a_url_and_lowercased(self) -> None:
        assert _host_of("HTTPS://URL1.Example.COM:443/a?b") == "url1.example.com"
        assert _host_of("https://user:pw@example.com/a") == "example.com"
        assert _host_of("not a url") is None

    def test_the_allowlist_is_deduplicated_and_sorted(self) -> None:
        request = a_request(
            candidate_urls=["https://example.com/a", "https://example.com/b", "https://x.test/c"]
        )
        assert _allowed_hosts(request) == ["example.com", "x.test"]


class TestTheSeam:
    def test_fake_mode_never_builds_the_real_runner(self) -> None:
        from motet_enrich.runner import build_runner

        runner = build_runner(load_settings({"MOTET_INFERENCE_MODE": "fake"}))
        assert isinstance(runner, FakeRunner)

    def test_a_real_runner_refuses_to_exist_without_its_toolchain(self) -> None:
        """A configuration fault in this process, not a run that went badly: it is the same
        for every request, and health has been saying so since startup."""
        with pytest.raises(RuntimeError, match="toolchain is not usable"):
            PiRunner(load_settings({"MOTET_INFERENCE_MODE": "real", "OPENROUTER_API_KEY": "k"}))


# --- the subprocess itself, driven over a stand-in `pi` ---------------------------------
#
# Everything above tests what is *handed* to the agent. This tests the machinery that runs
# it: the stream is consumed as it arrives, the caps kill the process group, a slow child
# is stopped by the wall clock, and a child that floods stderr does not deadlock the
# reader. None of it needs a model — what stands in for `pi` is a Python script emitting
# the JSON lines Pi's own `docs/json.md` documents.
#
# **The mode is written into the script rather than passed in the environment**, and that
# is not a convenience: `PiRunner._child_env` builds a deliberately minimal environment, so
# a variable this process exports does not reach the child at all. Having to write it into
# the file is the proof that the minimisation works.


def _answer(url: str) -> str:
    return (
        f"STATUS: ok\nLOGGED_IN: no\nARTICLE_URL: {url}\n\n"
        "```ARTICLE_MARKDOWN\n# Head\n\n" + ("Body. " * 80) + "\n```"
    )


ANSWER = _answer("https://example.com/articles/the-real-one")
OFFSITE_ANSWER = _answer("https://attacker.test/whatever")

FAKE_PI_BODY = """
def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()


emit({"type": "session", "version": 3, "id": "fake"})
emit({"type": "agent_start"})

if MODE == "noisy":
    # More than a pipe buffer's worth on stderr, written before anything else is read.
    for _ in range(4000):
        sys.stderr.write("chromium: [0913/182418:ERROR:dbus/bus.cc:405] noise\\n")
    sys.stderr.flush()

if MODE == "expensive":
    for index in range(50):
        emit({"type": "tool_execution_start", "toolCallId": str(index),
              "toolName": "browser__browser_execute", "args": {"code": "page.goto()"}})
        emit({"type": "message_update", "usage": {"cost": {"total": 0.05 * (index + 1)}}})
        time.sleep(0.02)
    emit({"type": "message_end", "message": {"role": "assistant",
          "content": [{"type": "text", "text": ANSWER}]}})
elif MODE == "slow":
    time.sleep(600)
elif MODE == "silent":
    sys.stderr.write("pi: no such model\\n")
    sys.exit(3)
else:
    emit({"type": "tool_execution_start", "toolCallId": "1",
          "toolName": "browser__browser_execute", "args": {"code": "page.goto('https://x')"}})
    emit({"type": "tool_execution_end", "toolCallId": "1",
          "toolName": "browser__browser_execute", "result": {"success": True}, "isError": False})
    emit({"type": "message_update", "usage": {"cost": {"total": 0.077}}})
    emit({"type": "message_end", "message": {"role": "assistant",
          "content": [{"type": "text", "text": ANSWER}]}})

emit({"type": "agent_end", "messages": []})
"""


def fake_pi(mode: str) -> str:
    """The stand-in `pi`, with its mode and its answer as literals."""
    header = (
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"MODE = {mode!r}\n"
        f"ANSWER = {(OFFSITE_ANSWER if mode == 'offsite' else ANSWER)!r}\n"
    )
    return header + FAKE_PI_BODY


@pytest.fixture
def runner_for(settings: EnrichSettings, toolchain: Path) -> Any:
    """Build a real :class:`PiRunner` whose `pi` prints a chosen event stream."""

    def build(mode: str) -> PiRunner:
        pi = toolchain / "node_modules" / ".bin" / "pi"
        pi.write_text(fake_pi(mode))
        pi.chmod(0o755)
        return PiRunner(settings)

    return build


class TestTheSubprocess:
    def test_a_successful_run_is_parsed_out_of_the_stream(self, runner_for: Any) -> None:
        result = runner_for("ok").run(
            a_request(), RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=60)
        )
        assert result.status == "ok"
        assert result.article_markdown is not None
        assert result.article_markdown.startswith("# Head")
        assert result.tool_calls == 1
        assert result.cost_usd == pytest.approx(0.077)
        assert [entry.kind for entry in result.transcript] == ["tool_call", "tool_result", "text"]

    def test_the_tool_call_cap_stops_the_run(self, runner_for: Any) -> None:
        """Pi has no "stop after N calls", so the runner reads the stream and kills."""
        result = runner_for("expensive").run(
            a_request(), RunCaps(max_usd=99.0, max_tool_calls=3, timeout_seconds=60)
        )
        assert result.status == "capped"
        assert result.tool_calls >= 3
        assert result.article_markdown is None
        assert result.error is not None and "cap" in result.error

    def test_the_dollar_cap_stops_the_run(self, runner_for: Any) -> None:
        result = runner_for("expensive").run(
            a_request(), RunCaps(max_usd=0.2, max_tool_calls=999, timeout_seconds=60)
        )
        assert result.status == "capped"
        assert result.cost_usd >= 0.2

    def test_the_wall_clock_stops_a_run_that_says_nothing(self, runner_for: Any) -> None:
        started = time.monotonic()
        result = runner_for("slow").run(
            a_request(), RunCaps(max_usd=1.0, max_tool_calls=40, timeout_seconds=2)
        )
        assert result.status == "timeout"
        assert time.monotonic() - started < 30

    def test_a_child_that_floods_stderr_does_not_deadlock_the_reader(self, runner_for: Any) -> None:
        """Chromium is loud on stderr, and a pipe nobody reads fills at 64 KiB and blocks
        the writer. Reading it only after the process exits would deadlock outright: the
        child waits for room on stderr while the reader waits for a line on stdout."""
        result = runner_for("noisy").run(
            a_request(), RunCaps(max_usd=1.0, max_tool_calls=40, timeout_seconds=30)
        )
        assert result.status == "ok"

    def test_a_run_that_produced_no_final_message_is_a_failure_with_the_tail(
        self, runner_for: Any
    ) -> None:
        result = runner_for("silent").run(
            a_request(), RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=30)
        )
        assert result.status == "failed"
        assert result.error is not None and "no such model" in result.error

    def test_the_run_directory_is_removed_whatever_happened(self, runner_for: Any) -> None:
        """It held the site password, every MCP bearer and the browser's cookies."""
        before = set(Path(tempfile.gettempdir()).glob("motet-enrich-*"))
        runner_for("ok").run(
            a_request(
                mcp_servers=[McpServer(name="cn-1", url="https://m.example/mcp", access_token="t")]
            ),
            RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=30),
        )
        assert set(Path(tempfile.gettempdir()).glob("motet-enrich-*")) == before


class TestTheArticlesProvenance:
    """`article_url` is a string from a model, and it lands in `source_items.text`."""

    def test_the_url_the_agent_reports_is_used_when_the_run_could_reach_it(
        self, runner_for: Any
    ) -> None:
        result = runner_for("ok").run(
            a_request(
                candidate_urls=[
                    "https://url1.example.com/ls/click?upn=a",
                    "https://url1.example.com/ls/click?upn=b",
                ]
            ),
            RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=30),
        )
        # The stand-in answers with a URL on the site, not with candidate 1.
        assert result.article_url == "https://example.com/articles/the-real-one"

    def test_a_url_outside_the_runs_hosts_is_refused_for_the_first_candidate(
        self, runner_for: Any
    ) -> None:
        """The one place a model's string would otherwise be written to the column every
        claim's source span is anchored into."""
        result = runner_for("offsite").run(
            a_request(), RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=30)
        )
        assert result.article_url == "https://url1.example.com/ls/click?upn=abc"


class TestTheStoredTranscript:
    def test_the_article_is_not_stored_twice(self, runner_for: Any) -> None:
        """It is already `source_items.text`; storing it again doubles the largest row in
        the table and is the one part of the answer with no forensic value."""
        result = runner_for("ok").run(
            a_request(), RunCaps(max_usd=0.5, max_tool_calls=40, timeout_seconds=30)
        )
        text = next(entry.text for entry in result.transcript if entry.kind == "text")
        assert text is not None
        assert "STATUS: ok" in text
        assert "Body." not in text
        assert "<article, stored on the item>" in text
