"""The real runner: the Pi CLI, a stealth browser, and the owner's own MCP servers.

**This is a dormant path in this repository, in the sense AGENTS.md already uses for Gmail
and for the KMS vault.** Every test runs the fake; ``bin/build-images enrich`` proves the
toolchain is in the image and that the service answers without it doing anything; the
first real run is the owner's, against the owner's own paywalled site. What is pinned
offline is the *configuration* this file writes — the MCP document, the model row, the
argv, the environment each child gets — because a typo in any of those ships green and
fails at the vendor.

Five things about it are decisions rather than implementation.

**One directory per run, 0700, deleted in a ``finally``.** It holds the MCP config, the
model catalogue, one 0600 bearer file per MCP server, and the browser's storage state in
and out. Nothing in it outlives the request, and nothing in it is in a shared location two
concurrent runs could collide on — though Cloud Run's concurrency for this service is 1,
so in the deployment there is only ever one.

**Its own process group, and the kill is of the group.** ``pi`` starts the MCP adapter,
which starts the browser server, which starts Chromium. Signalling the pid the caller
holds reaches ``pi`` and orphans a Chromium holding half a gigabyte — the same argument
``tools/dev.py`` makes about ``uv run`` wrapping ``uvicorn``, one toolchain along.

**The caps are enforced here, from the event stream, not by asking Pi to enforce them.**
Pi has no "stop after N tool calls" and no dollar budget; what it has is a JSON line per
event carrying the cumulative usage, including cost. So this reads the stream as it
arrives and kills the group the moment a cap is passed. A cap is a *recorded skip* — the
answer comes back as ``capped`` with whatever the run had got to, including the cookies.

**The browser server gets a deliberately tiny environment, and that is a control.**
``browser_execute`` evaluates the model's JavaScript in the MCP server's *Node* process —
``new AsyncFunction('page', code)`` — so anything in that process's environment is
readable by whatever the model was talked into writing, and third-party page text is in
its context. That process therefore never sees ``OPENROUTER_API_KEY``, the service token,
or any MCP bearer. It sees the paths it needs and the site password, which is the one
secret that has to be typeable into a form.

**Say the honest thing about what that bounds and what it does not.** A model with
arbitrary JavaScript in a Node process can `await import(...)` anything Node can reach; the
navigation lock in the harness stops the *browser* leaving the site, not the process. The
boundary that actually holds is the one design option D2 bought: this container's service
account holds nothing — no KMS, no database, no bucket — so the worst an injected
instruction can reach is the credentials this one run was handed, which are one site's
login, one browser's cookies and the MCP servers the owner connected on purpose.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess  # noqa: S404 — running the agent is this module's whole job
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO, Any, Final

from motet_inference.llm import KNOWN_MODELS

from .config import EnrichSettings
from .contract import EnrichRequest, EnrichResult, RunCaps, RunStatus, TranscriptEntry
from .prompt import SYSTEM_PROMPT, build_prompt, parse_answer
from .redact import (
    Redactor,
    is_browser_tool,
    strip_article,
    summarize_foreign_result,
)

logger = logging.getLogger("motet.enrich.pi")

#: The slug the browser server is registered under. ``motet_enrich.redact.BROWSER_SERVER``
#: is the same string and is what decides whether a tool result may be stored, so the two
#: are pinned to each other by a test rather than left to agree by habit.
BROWSER_SERVER_NAME: Final = "browser"

#: How long to wait after SIGTERM before SIGKILLing the group.
GRACE_SECONDS: Final = 5.0

#: OpenRouter, under a name of our own rather than pi's built-in ``openrouter``. Pi ships a
#: catalogue that does not carry this repo's models, and overriding a built-in provider
#: merges rather than replaces — so a fresh name is the shape where what this file writes
#: is exactly what runs.
PROVIDER_NAME: Final = "motet-openrouter"
OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1"

#: Environment variable the harness reads the site password out of. It is set on the
#: browser server's process and on nothing else.
SITE_PASSWORD_ENV: Final = "MOTET_SITE_PASSWORD"
STORAGE_STATE_IN_ENV: Final = "MOTET_STORAGE_STATE_IN"
STORAGE_STATE_OUT_ENV: Final = "MOTET_STORAGE_STATE_OUT"
ALLOWED_HOSTS_ENV: Final = "MOTET_ALLOWED_HOSTS"


class PiRunner:
    """Drive one Pi session over a stealth browser and the run's MCP servers."""

    def __init__(self, settings: EnrichSettings) -> None:
        self._settings = settings
        toolchain = settings.toolchain
        if not toolchain.ready:
            # A configuration fault in this process, not a run that went badly: it is the
            # same for every request and retrying costs the caller a round trip to learn
            # the same thing. `/internal/health` has been saying so since startup.
            raise RuntimeError(f"the enrichment toolchain is not usable — {toolchain.detail}")
        assert toolchain.pi is not None
        assert toolchain.adapter is not None
        assert toolchain.harness is not None
        self._pi = toolchain.pi
        self._adapter = toolchain.adapter
        self._harness = toolchain.harness

    # --- the run ---------------------------------------------------------------------

    def run(self, request: EnrichRequest, caps: RunCaps) -> EnrichResult:
        started = time.monotonic()
        redact = Redactor(
            [request.site.password, request.site.username]
            + [server.access_token for server in request.mcp_servers]
        )
        workdir = Path(tempfile.mkdtemp(prefix="motet-enrich-"))
        os.chmod(workdir, 0o700)
        state_out = workdir / "storage-state.out.json"
        try:
            self._write_layout(workdir, request)
            collected = self._execute(workdir, request, caps)
            duration = time.monotonic() - started
            return self._assemble(collected, request, redact, state_out, duration)
        except Exception as exc:  # noqa: BLE001 — a failed run is an answer, not a 500
            logger.exception("enrichment run for %s failed", request.item_id)
            return EnrichResult(
                status="failed",
                tool_calls=0,
                duration_seconds=time.monotonic() - started,
                browser_state=_read_if_present(state_out),
                error=redact(f"{type(exc).__name__}: {exc}"),
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- the files one run needs -----------------------------------------------------

    def _write_layout(self, workdir: Path, request: EnrichRequest) -> None:
        (workdir / ".pi").mkdir()
        (workdir / "agent").mkdir()
        _write(workdir / "agent" / "models.json", json.dumps(self._models_document(), indent=2))
        if request.browser_state:
            _write(workdir / "storage-state.in.json", request.browser_state)
        for server in request.mcp_servers:
            # The bearer never goes in the MCP document. The adapter runs a `!command` for
            # a header value at connect time, so what is on disk in a config a subprocess
            # reads is a path, and the token itself is in a 0600 file this process wrote.
            _write(workdir / f"bearer-{server.name}", f"Bearer {server.access_token}\n", mode=0o600)
            _write(
                workdir / f"bearer-{server.name}.sh",
                f'#!/bin/sh\nexec cat "$(dirname "$0")/bearer-{server.name}"\n',
                mode=0o700,
            )
        _write(
            workdir / ".pi" / "mcp.json",
            json.dumps(self._mcp_document(workdir, request), indent=2),
        )

    def _models_document(self) -> dict[str, Any]:
        """Pi's model catalogue for this run — one provider, one model, priced.

        Priced from :data:`motet_inference.llm.KNOWN_MODELS`, which is the same row
        ``motet.llm.tokens`` and the spend ledger are priced from and which
        ``bin/check-openrouter-models`` drift-checks against OpenRouter's live list. A
        second hand-written price here is a second number to be wrong.
        """
        spec = KNOWN_MODELS[self._settings.model]
        return {
            "providers": {
                PROVIDER_NAME: {
                    "baseUrl": OPENROUTER_BASE_URL,
                    "api": "openai-completions",
                    # Interpolated by pi from the child environment below, so the key
                    # itself is never written to a file a subprocess could read.
                    "apiKey": "$OPENROUTER_API_KEY",
                    "authHeader": True,
                    "models": [
                        {
                            "id": spec.slug,
                            "name": spec.slug,
                            "reasoning": bool(spec.efforts),
                            "contextWindow": spec.context_tokens,
                            "maxTokens": spec.max_output_tokens,
                            "cost": {
                                "input": spec.input_usd_per_mtok,
                                "output": spec.output_usd_per_mtok,
                                "cacheRead": spec.cache_read_usd_per_mtok,
                                "cacheWrite": spec.cache_write_usd_per_mtok,
                            },
                        }
                    ],
                }
            }
        }

    def _mcp_document(self, workdir: Path, request: EnrichRequest) -> dict[str, Any]:
        """The browser server, plus whichever of the owner's servers this run may use.

        ``directTools`` so each tool appears in the agent's own tool list rather than
        behind the adapter's proxy tool: the agent makes a handful of calls and the proxy's
        discovery round trip is a turn's worth of tokens for nothing.
        """
        servers: dict[str, Any] = {
            BROWSER_SERVER_NAME: {
                "command": "node",
                "args": [str(self._harness)],
                "env": self._browser_env(workdir, request),
                # The browser server must NOT inherit this process's environment: that is
                # where OPENROUTER_API_KEY and the service token live, and the model can
                # read the environment of the process it evaluates JavaScript in.
                "inheritEnv": False,
            }
        }
        for server in request.mcp_servers:
            if server.name == BROWSER_SERVER_NAME:
                # Reserved. Without this, a request naming a server `browser` replaces the
                # harness entry outright — no navigation lock, no storage state, no
                # `inheritEnv: False` — and every tool it exposed would read as a browser
                # tool to `motet_enrich.redact.is_browser_tool` and have its results
                # stored. Unreachable from the worker, which derives the slug from the
                # connector id; refused here because the contract is the boundary.
                raise ValueError(f"{BROWSER_SERVER_NAME!r} is reserved for the browser server")
            servers[server.name] = {
                "url": server.url,
                "headers": {"Authorization": f"!{workdir / f'bearer-{server.name}.sh'}"},
            }
        return {"mcpServers": servers, "directTools": True}

    def _browser_env(self, workdir: Path, request: EnrichRequest) -> dict[str, str]:
        """Everything the browser server gets, and it is a short list on purpose."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(workdir),
            "NODE_PATH": str(self._settings.toolchain.root / "node_modules"),
            "STEALTH_MODE": "true",
            "HEADLESS": "true",
            # Certificate validation stays on. The server defaults it *off* for
            # convenience in containers, which on the open internet is the difference
            # between a paywall and anyone on the path reading the owner's session.
            "IGNORE_HTTPS_ERRORS": "false",
            "TIMEOUT": "30000",
            ALLOWED_HOSTS_ENV: ",".join(_allowed_hosts(request)),
            STORAGE_STATE_OUT_ENV: str(workdir / "storage-state.out.json"),
        }
        browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if browsers:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
        if request.browser_state:
            env[STORAGE_STATE_IN_ENV] = str(workdir / "storage-state.in.json")
        if request.site.password:
            env[SITE_PASSWORD_ENV] = request.site.password
        return env

    # --- the subprocess --------------------------------------------------------------

    def _argv(self, request: EnrichRequest) -> list[str]:
        thinking = self._settings.thinking
        model = f"{self._settings.model}:{thinking}" if thinking != "off" else self._settings.model
        return [
            self._pi,
            "--print",
            "--mode",
            "json",
            "--provider",
            PROVIDER_NAME,
            "--model",
            model,
            # No read/write/edit/bash: this agent's only capability is the tools it was
            # handed, and a file-editing tool in a container holding a live session is a
            # capability nothing in this task needs.
            "--no-builtin-tools",
            # Nothing on this machine is a project of the agent's. `--no-session` keeps it
            # from writing one, and the three discovery switches keep it from picking up
            # anything a future image happens to leave in the working directory.
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--offline",
            "--extension",
            str(self._adapter),
            "--system-prompt",
            SYSTEM_PROMPT,
            "--",
            build_prompt(request),
        ]

    def _child_env(self, workdir: Path) -> dict[str, str]:
        """Pi's own environment: the vendor key, and the agent directory for this run."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(workdir),
            "PI_CODING_AGENT_DIR": str(workdir / "agent"),
            "OPENROUTER_API_KEY": self._settings.openrouter_key,
            "PI_OFFLINE": "1",
            "NO_COLOR": "1",
        }
        for name in ("PLAYWRIGHT_BROWSERS_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        return env

    def _execute(self, workdir: Path, request: EnrichRequest, caps: RunCaps) -> _Collected:
        collected = _Collected()
        # `start_new_session` is the process group: `pi` starts the adapter, which starts
        # the browser server, which starts Chromium, and only a group signal reaches all
        # four. stdin from /dev/null because pi in print mode reads stdin and hangs under
        # a harness that leaves a pipe open — one of the spike's recorded traps.
        with subprocess.Popen(  # noqa: S603 — argv is built here, never from the caller
            self._argv(request),
            cwd=workdir,
            env=self._child_env(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        ) as proc:
            # **Stderr is drained by a thread, and that is not tidiness.** Chromium is
            # loud on stderr — dbus, bluetooth, GPU — and a pipe nobody reads fills at
            # 64 KiB and blocks the writer. Reading it only after the process exits would
            # therefore deadlock: the child waits for room on stderr while this thread
            # waits for a line on stdout that will never come. Only the tail is kept,
            # because its one consumer is the "the agent produced no final message" error.
            stderr = collections.deque[str](maxlen=100)
            drain = threading.Thread(
                target=_drain, args=(proc.stderr, stderr), daemon=True, name="enrich-stderr"
            )
            drain.start()
            timer = threading.Timer(
                caps.timeout_seconds, lambda: self._stop(proc, collected, "timeout")
            )
            timer.daemon = True
            timer.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    self._consume(line, collected, caps, proc)
            finally:
                timer.cancel()
                self._stop(proc, collected, collected.stop_reason or "")
                drain.join(timeout=GRACE_SECONDS)
                # `.copy()` rather than iterating: `_drain` is joined with a *timeout*, so
                # it may still be appending, and iterating a deque under mutation raises.
                collected.stderr = "".join(stderr.copy())[-4_000:]
        collected.exit_code = proc.returncode
        return collected

    def _consume(
        self, line: str, collected: _Collected, caps: RunCaps, proc: subprocess.Popen[str]
    ) -> None:
        event = _parse_line(line)
        if event is None:
            return
        collected.absorb(event)
        if collected.tool_calls > caps.max_tool_calls:
            self._stop(proc, collected, "capped")
        elif collected.cost_usd > caps.max_usd:
            self._stop(proc, collected, "capped")

    def _stop(self, proc: subprocess.Popen[str], collected: _Collected, reason: str) -> None:
        """Signal the whole group, then make sure of it.

        Idempotent: the deadline timer and the cap check can both fire, and the ``finally``
        calls it a third time on the ordinary path where the process has already exited.
        """
        if reason and collected.stop_reason is None:
            collected.stop_reason = reason
        if proc.poll() is not None:
            return
        try:
            group = os.getpgid(proc.pid)
        except ProcessLookupError:  # pragma: no cover — it exited between the two lines
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGTERM)
        deadline = time.monotonic() + GRACE_SECONDS
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            logger.warning("the agent did not stop on SIGTERM; killing the group")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)

    # --- the answer ------------------------------------------------------------------

    def _assemble(
        self,
        collected: _Collected,
        request: EnrichRequest,
        redact: Redactor,
        state_out: Path,
        duration: float,
    ) -> EnrichResult:
        """One run's stream, turned into the answer — and there is exactly one shape of it.

        Written as a single constructor with the varying parts computed above it, rather
        than four early returns sharing a ``**base``: every outcome carries the cost, the
        tool calls and the cookies, and the one thing this function must not be able to do
        is answer ``failed`` while quietly dropping the login the run paid for.
        """
        status, login, article, url, error = self._verdict(collected, redact)
        return EnrichResult(
            status=status,
            article_url=_article_url(url, request) if status == "ok" else None,
            article_markdown=article,
            login_performed=login,
            tool_calls=collected.tool_calls,
            cost_usd=round(collected.cost_usd, 6),
            duration_seconds=round(duration, 3),
            browser_state=_read_if_present(state_out),
            transcript=list(_redacted(collected.entries, redact)),
            error=error,
        )

    def _verdict(
        self, collected: _Collected, redact: Redactor
    ) -> tuple[RunStatus, bool, str | None, str | None, str | None]:
        """``(status, login, article, article_url, error)`` — how the run ended, and why.

        Every ``error`` this returns has been through ``redact``: the two that are literals
        carry nothing, and the two built from the run carry whatever the agent or its
        toolchain said.
        """
        if collected.stop_reason == "timeout":
            return "timeout", False, None, None, "the run was stopped by its wall clock"
        if collected.stop_reason == "capped":
            return "capped", False, None, None, "the run passed its cost or tool-call cap"
        if collected.final_text is None:
            return (
                "failed",
                False,
                None,
                None,
                redact(
                    f"the agent produced no final message (exit {collected.exit_code}): "
                    f"{collected.stderr.strip()[-500:]}"
                ),
            )
        answered, login, article, url = parse_answer(collected.final_text)
        if answered != "ok" or article is None:
            return (
                "blocked",
                login,
                None,
                None,
                redact((strip_article(collected.final_text) or "").strip()[-500:]),
            )
        return "ok", login, article, url, None


# --- the event stream --------------------------------------------------------------------


class _Collected:
    """What one run's JSON-lines stream adds up to."""

    def __init__(self) -> None:
        self.tool_calls = 0
        self.cost_usd = 0.0
        self.final_text: str | None = None
        self.stop_reason: str | None = None
        self.stderr: str = ""
        self.exit_code: int | None = None
        self.entries: list[TranscriptEntry] = []
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def absorb(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            cost = usage.get("cost")
            if isinstance(cost, Mapping) and isinstance(cost.get("total"), int | float):
                # Cumulative, not per event — so the maximum is the run's cost even if a
                # later event's accounting arrives partial.
                self.cost_usd = max(self.cost_usd, float(cost["total"]))
        if kind == "tool_execution_start":
            self.tool_calls += 1
            self.entries.append(
                TranscriptEntry(
                    seq=self._next(),
                    kind="tool_call",
                    tool=str(event.get("toolName") or ""),
                    args=_stringify(event.get("args")),
                )
            )
        elif kind == "tool_execution_end":
            tool = str(event.get("toolName") or "")
            self.entries.append(
                TranscriptEntry(
                    seq=self._next(),
                    kind="tool_result",
                    tool=tool,
                    ok=not event.get("isError"),
                    result=_stringify(event.get("result")),
                )
            )
        elif kind == "message_end":
            text = _assistant_text(event.get("message"))
            if text:
                self.final_text = text
                self.entries.append(
                    TranscriptEntry(
                        seq=self._next(), kind="text", text=text, cost_usd=self.cost_usd
                    )
                )


def _redacted(entries: list[TranscriptEntry], redact: Redactor) -> Iterator[TranscriptEntry]:
    """Apply both rules from :mod:`motet_enrich.redact`, in that module's order."""
    for entry in entries:
        result = entry.result
        if entry.kind == "tool_result" and not is_browser_tool(entry.tool):
            result = summarize_foreign_result(entry.tool or "an unnamed tool", result)
        else:
            result = redact.clip(result)
        yield entry.model_copy(
            update={
                "args": redact.clip(entry.args),
                "result": result,
                # The article comes out before anything else: it is already on the item,
                # and rule 1 does not reach the model's own text. See `motet_enrich.redact`.
                "text": redact.clip(strip_article(entry.text)),
            }
        )


def _drain(stream: IO[str] | None, into: collections.deque[str]) -> None:
    """Read a pipe to EOF, keeping only the tail. See :meth:`PiRunner._execute`."""
    if stream is None:
        return
    with contextlib.suppress(ValueError, OSError):
        for line in stream:
            into.append(line)


def _parse_line(line: str) -> Mapping[str, Any] | None:
    """One stdout line, or ``None`` if it is not one of ours.

    Pi's stream is JSON lines, but a dependency that writes to stdout anyway would
    otherwise abort the run at the first `json.JSONDecodeError`. Ignoring an unparseable
    line is the right failure here: what it costs is one missing transcript entry, and what
    the alternative costs is the whole article.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _assistant_text(message: Any) -> str | None:
    """The text of an assistant message, whatever shape the content came in."""
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    parts = [
        str(part.get("text", ""))
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    ]
    joined = "\n".join(part for part in parts if part)
    return joined or None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _article_url(claimed: str | None, request: EnrichRequest) -> str:
    """Which URL the article came from, believed only if it is one the run could reach.

    Up to :data:`MAX_CANDIDATE_URLS` candidates go to the agent and the prompt tells it to
    pick, so assuming the first was simply wrong: this string becomes the provenance line in
    ``source_items.text``, which is the column every claim's span is anchored into. But it
    *is* a string from a model, so it is only taken when its host is one the navigation lock
    allowed — otherwise the first candidate, which is at least a URL the newsletter carried.
    """
    if claimed:
        host = _host_of(claimed)
        allowed = _allowed_hosts(request)
        if host and any(host == entry or host.endswith(f".{entry}") for entry in allowed):
            return claimed
        logger.warning("the agent reported an article URL outside this run's hosts; ignoring it")
    return request.candidate_urls[0]


def _allowed_hosts(request: EnrichRequest) -> list[str]:
    """Where the browser may navigate: the site, and the links the newsletter carried.

    The site's domain covers its subdomains — a publisher-branded click-tracking host such
    as ``url3396.example.com`` is one — and each candidate URL's own host is added because
    a newsletter's link may start on a host that is not the publisher's at all. Nothing
    else is ever added: a host discovered *on a page* is exactly what an injected
    instruction would name.
    """
    hosts = {request.site.domain}
    for url in request.candidate_urls:
        host = _host_of(url)
        if host:
            hosts.add(host)
    return sorted(hosts)


_HOST_RE: Final = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", re.IGNORECASE)


def _host_of(url: str) -> str | None:
    match = _HOST_RE.match(url.strip())
    if match is None:
        return None
    authority = match.group(1).split("@")[-1]
    return authority.split(":")[0].lower().strip(".") or None


def _write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Create the file **at** its mode, never at the umask and then chmod.

    ``write_text`` followed by ``chmod`` leaves an MCP bearer and the incoming storage state
    world-readable for the interval between the two calls, which is not what
    :meth:`PiRunner._write_layout` says it does. ``os.open`` takes the mode at creation.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    # An existing file keeps its own mode through O_CREAT, so say it again for the case
    # where a run directory somehow held one.
    os.chmod(path, mode)


def _read_if_present(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") or None
    except OSError:
        return None
