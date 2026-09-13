"""PROTOTYPE — agentic enrichment: a coding agent fetches the full article behind a preview.

``handle_integrate`` runs triage first (``handlers._triage``); when it says a source item is
only a preview, it queues an ``enrich`` job here and returns. This handler resolves the
user's credentials, runs one Pi session driving a headless browser and the user's MCP
servers, writes the article over the preview (keeping the preview in ``original_text``),
records the run with a **redacted** transcript, and queues integrate again with
``enriched: true`` so triage is skipped the second time. On failure or timeout it records
that and *still* queues integrate — the owner's decision: the preview is better than
nothing, and a login every time is acceptable while this is a prototype.

Three seams, each with the same shape as the rest of the repo:

* :class:`PiRunner` is the vendor seam — the real one shells out to ``pi`` exactly as the
  spike in ``proto/enrich-spike/`` does; the fake returns a canned outcome and no test here
  starts a process (invariant 7). :func:`build_pi_runner` picks by ``MOTET_INFERENCE_MODE``.
* The vault is opened with the full :class:`~motet_vault.KeyManager` — this is the worker,
  and it is the decrypt boundary (invariant 8): the site password, the MCP bearer tokens
  and the saved browser cookies are all read here and nowhere else.
* Secrets reach the agent through files in a per-run ``0700`` directory that is deleted
  afterwards: the MCP bearer through a ``!command`` header hook that ``cat``s a ``0600``
  file, the cookies through ``MOTET_STORAGE_STATE_IN`` read by the harness's own browser
  server (``enrich_harness/browser-mcp.mjs``) — never through the prompt, which the spike
  found cost most of a run. The site password is the one credential that has to travel in
  the prompt, and it is scrubbed from the transcript by exact match before anything is
  stored.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import psycopg
from motet_db import connectors, enrichment, repo
from motet_db.connectors import StoredConnector
from motet_inference.llm import KNOWN_MODELS
from motet_inference.mode import current_mode
from motet_vault import KeyManager, VaultError, build_key_manager
from opentelemetry import metrics

from .jobs import enqueue
from .queues import Queue

if TYPE_CHECKING:
    from .handlers import Context

logger = logging.getLogger("motet.worker.enrich")

_meter = metrics.get_meter("motet.worker")
_runs = _meter.create_counter(
    "motet.enrich.runs",
    unit="{run}",
    description="Agentic enrichment runs, by outcome (ok, blocked, failed, timeout).",
)
_cost = _meter.create_counter(
    "motet.enrich.cost_usd",
    unit="USD",
    description="What the enrichment agent's own accounting says its runs cost.",
)

#: A whole run — Pi, the browser, the login dance — has this long before the process group
#: is killed and the item integrates on its preview.
ENRICH_TIMEOUT_SECONDS: Final = 600

#: Fewer characters than this is not an article; a "Subscribe to read" stub is ~1,200.
MIN_ARTICLE_CHARS: Final = 400

TOOLCHAIN_ENV: Final = "MOTET_ENRICH_TOOLCHAIN_DIR"
MODEL_ENV: Final = "MOTET_ENRICH_MODEL"
THINKING_ENV: Final = "MOTET_ENRICH_THINKING"
DEFAULT_MODEL: Final = "anthropic/claude-sonnet-5"
DEFAULT_THINKING: Final = "low"

HARNESS_DIR: Final = Path(__file__).parent / "enrich_harness"

#: A bearer that expires inside this window is refreshed before the run rather than during it.
REFRESH_MARGIN: Final = timedelta(seconds=60)

#: Characters of a transcript field kept, so a run's record stays a few tens of KB.
TRANSCRIPT_FIELD_CHARS: Final = 600
ASSISTANT_TEXT_CHARS: Final = 2_000

RunStatus = Literal["ok", "blocked", "failed", "timeout"]


# --- the seam ------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerSpec:
    """One remote MCP server to hand the agent, with its bearer already resolved."""

    name: str
    label: str
    url: str
    bearer: str

    def __repr__(self) -> str:
        return f"McpServerSpec(name={self.name!r}, label={self.label!r}, bearer=<redacted>)"


@dataclass(frozen=True)
class EnrichRequest:
    source_item_id: str
    user_id: str
    article_url: str
    domain: str
    login_email: str | None
    login_password: str | None
    mcp_servers: tuple[McpServerSpec, ...]
    storage_state_json: str | None
    timeout_seconds: int = ENRICH_TIMEOUT_SECONDS

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every literal that must never appear in a stored transcript."""
        return tuple(
            s
            for s in (self.login_password, *(m.bearer for m in self.mcp_servers))
            if s and len(s) >= 6
        )

    def __repr__(self) -> str:
        return (
            f"EnrichRequest(source_item_id={self.source_item_id!r}, domain={self.domain!r}, "
            f"mcp_servers={len(self.mcp_servers)}, "
            f"saved_state={self.storage_state_json is not None}, credentials=<redacted>)"
        )


@dataclass(frozen=True)
class EnrichOutcome:
    """What one run produced. ``transcript`` is raw — :func:`redact_transcript` runs after."""

    status: RunStatus
    article_markdown: str = ""
    logged_in: str | None = None
    notes: str = ""
    tool_calls: int = 0
    cost_usd: float | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    storage_state_json: str | None = None
    error: str | None = None

    @property
    def login_performed(self) -> bool:
        return self.logged_in == "yes"


class PiRunner(Protocol):
    def run(self, request: EnrichRequest) -> EnrichOutcome: ...


class FakePiRunner:
    """A scripted run: returns ``outcome`` and remembers what it was asked."""

    def __init__(self, outcome: EnrichOutcome | None = None) -> None:
        self.outcome = outcome or EnrichOutcome(
            status="blocked", notes="fake runner: no agent in fake mode", error="fake mode"
        )
        self.requests: list[EnrichRequest] = []

    def run(self, request: EnrichRequest) -> EnrichOutcome:
        self.requests.append(request)
        return self.outcome


def build_pi_runner() -> PiRunner:
    """The subprocess runner in real mode, the fake otherwise — the same rule as every seam."""
    if current_mode() == "real":
        return SubprocessPiRunner()
    return FakePiRunner()


# --- the handler ---------------------------------------------------------------------


def handle_enrich(context: Context, payload: Mapping[str, Any]) -> None:
    """Run the agent for one source item and hand the item back to integrate.

    Idempotent on replay: an item whose enrichment is already ``done`` or ``failed`` has
    had its integrate job queued in the same transaction, so there is nothing to do.
    """
    source_item_id = str(payload.get("source_item_id") or "")
    current = enrichment.get_enrichment(context.conn, source_item_id)
    if not source_item_id or current is None:
        raise _permanent(f"source item {source_item_id!r} no longer exists")
    if current.enrich_status in ("done", "failed"):
        logger.info(
            "source item %s enrichment is already %s; nothing to do",
            source_item_id,
            current.enrich_status,
        )
        return
    article_url = str(payload.get("article_url") or current.article_url or "")
    if not article_url:
        raise _permanent(f"source item {source_item_id} has no article URL to fetch")
    domain = connectors.normalize_domain(str(payload.get("domain") or "") or article_url)

    _mark_running(source_item_id)
    manager = build_key_manager()
    request = _build_request(context.conn, manager, current, article_url=article_url, domain=domain)
    logger.info(
        "enrich: source item %s → %s (site login: %s, mcp servers: %d, saved cookies: %s)",
        source_item_id,
        request.domain,
        "yes" if request.login_email else "none",
        len(request.mcp_servers),
        "yes" if request.storage_state_json else "no",
    )

    started = datetime.now(UTC)
    outcome = build_pi_runner().run(request)
    finished = datetime.now(UTC)
    _finish(
        context.conn,
        manager,
        current,
        request,
        outcome,
        started=started,
        finished=finished,
        article_url=article_url,
    )


def _finish(
    conn: psycopg.Connection[Any],
    manager: KeyManager,
    current: enrichment.Enrichment,
    request: EnrichRequest,
    outcome: EnrichOutcome,
    *,
    started: datetime,
    finished: datetime,
    article_url: str,
) -> None:
    source_item_id = current.source_item_id
    if outcome.storage_state_json:
        try:
            cookies = enrichment.store_browser_state(
                conn,
                manager,
                user_id=current.user_id,
                domain=request.domain,
                state_json=outcome.storage_state_json,
            )
            logger.info("enrich: sealed %d cookies for %s", cookies, request.domain)
        except (ValueError, VaultError):
            logger.warning("enrich: could not seal the browser state for %s", request.domain)

    transcript = redact_transcript(outcome.transcript, request.secrets)
    article = outcome.article_markdown.strip()
    ok = outcome.status == "ok" and len(article) >= MIN_ARTICLE_CHARS
    if outcome.status == "ok" and not ok:
        error: str | None = f"agent reported ok but returned {len(article)} chars of article"
    else:
        error = outcome.error or (
            None if ok else f"{outcome.status}: {outcome.notes or 'no article'}"
        )

    if ok:
        text = f"Full article fetched from {article_url}\n\n{article}"
        enrichment.apply_enrichment(conn, source_item_id, article_text=text)
    else:
        enrichment.set_enrich_status(conn, source_item_id, "failed", error=(error or "")[:2000])

    enrichment.insert_enrich_run(
        conn,
        source_item_id=source_item_id,
        user_id=current.user_id,
        started_at=started,
        finished_at=finished,
        status="done" if ok else "failed",
        tool_calls=outcome.tool_calls,
        cost_usd=outcome.cost_usd,
        transcript=transcript,
        article_chars=len(article) if ok else 0,
        login_performed=outcome.login_performed,
        error=error,
    )
    _runs.add(1, {"motet.enrich.outcome": outcome.status if not ok else "ok"})
    if outcome.cost_usd:
        _cost.add(outcome.cost_usd, {"motet.enrich.domain": request.domain})
    logger.info(
        "enrich: source item %s %s — %d tool call(s), $%.4f, login %s, %d article chars, %.0fs%s",
        source_item_id,
        "done" if ok else f"failed ({outcome.status})",
        outcome.tool_calls,
        outcome.cost_usd or 0.0,
        outcome.logged_in or "unknown",
        len(article) if ok else 0,
        (finished - started).total_seconds(),
        f": {error}" if error else "",
    )
    # Either way the item integrates — on the article, or on the preview it already had.
    enqueue(
        conn,
        Queue.INTEGRATE,
        {"source_item_id": source_item_id, "enriched": True},
        serialize_key=current.user_id,
    )


def enrich_failed(conn: psycopg.Connection[Any], payload: Mapping[str, Any], error: str) -> None:
    """The failure recorder: an enrichment that exhausted its retries still integrates."""
    source_item_id = payload.get("source_item_id")
    if not isinstance(source_item_id, str) or not source_item_id:
        return
    current = enrichment.get_enrichment(conn, source_item_id)
    if current is None or current.enrich_status in ("done", "failed"):
        return
    enrichment.set_enrich_status(conn, source_item_id, "failed", error=error[:2000])
    enqueue(
        conn,
        Queue.INTEGRATE,
        {"source_item_id": source_item_id, "enriched": True},
        serialize_key=current.user_id,
    )


def _mark_running(source_item_id: str) -> None:
    """``enrich_status = running`` on a connection of its own, so the UI sees it now.

    The handler's own transaction stays open for the whole run — up to ten minutes — and
    nothing written on it is visible until then. Best-effort: a deployment without
    ``DATABASE_URL`` in the environment simply shows ``pending`` until the run settles.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    try:
        with repo.connect(url, connect_timeout=10) as side:
            side.autocommit = True
            enrichment.set_enrich_status(side, source_item_id, "running")
    except Exception:  # noqa: BLE001 — a status marker must not stop the run
        logger.warning("enrich: could not mark %s running", source_item_id, exc_info=True)


# --- credentials -----------------------------------------------------------------------


def _build_request(
    conn: psycopg.Connection[Any],
    manager: KeyManager,
    current: enrichment.Enrichment,
    *,
    article_url: str,
    domain: str,
) -> EnrichRequest:
    rows = connectors.list_connectors(conn, current.user_id)
    site = matching_site(rows, domain)
    if site is not None and site.domain:
        # The credential's domain is the site: a tracking-link host such as
        # `url3396.theinformation.com` matched it by suffix, and the cookies belong to the
        # site rather than to whichever click-tracking subdomain the newsletter used.
        domain = connectors.normalize_domain(site.domain)
    login_email = site.username if site else None
    login_password = (
        connectors.load_connector_secret(conn, manager, connector_id=site.id) if site else None
    )
    servers = tuple(
        spec
        for row in rows
        if row.kind == connectors.MCP and row.status == "ready" and row.has_secret
        if _mcp_wanted(row, domain)
        if (spec := _mcp_server(conn, manager, row)) is not None
    )
    state = None
    try:
        saved = enrichment.load_browser_state(conn, manager, user_id=current.user_id, domain=domain)
    except VaultError:
        logger.warning("enrich: the saved browser state for %s did not open; ignoring", domain)
        saved = None
    if saved is not None:
        state = saved.state_json
        logger.info("enrich: reusing %d saved cookies for %s", saved.cookies, domain)
    return EnrichRequest(
        source_item_id=current.source_item_id,
        user_id=current.user_id,
        article_url=article_url,
        domain=domain,
        login_email=login_email,
        login_password=login_password or None,
        mcp_servers=servers,
        storage_state_json=state,
    )


def matching_site(rows: Sequence[StoredConnector], domain: str) -> StoredConnector | None:
    """The ``site`` connector for ``domain`` — exact, or a parent domain of it."""
    wanted = connectors.normalize_domain(domain)
    for row in rows:
        if row.kind != connectors.SITE or not row.domain:
            continue
        own = connectors.normalize_domain(row.domain)
        if wanted == own or wanted.endswith("." + own):
            return row
    return None


def _mcp_wanted(row: StoredConnector, domain: str) -> bool:
    if not row.domains:
        return True
    wanted = connectors.normalize_domain(domain)
    return any(
        wanted == connectors.normalize_domain(d)
        or wanted.endswith("." + connectors.normalize_domain(d))
        for d in row.domains
    )


def _mcp_server(
    conn: psycopg.Connection[Any], manager: KeyManager, row: StoredConnector
) -> McpServerSpec | None:
    try:
        bearer = mcp_access_token(conn, manager, row)
    except Exception:  # noqa: BLE001 — one server's bad token must not stop the run
        logger.warning(
            "enrich: connector %s (%s) has no usable token; running without it",
            row.id,
            row.label,
            exc_info=True,
        )
        return None
    if bearer is None or not row.url:
        return None
    return McpServerSpec(name=mcp_server_name(row), label=row.label, url=row.url, bearer=bearer)


def mcp_server_name(row: StoredConnector) -> str:
    """The name the adapter prefixes the server's tools with: ``email`` for a mailbox."""
    label = row.label.lower()
    if "mail" in label:
        return "email"
    slug = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
    return slug or f"mcp_{row.id[-6:]}"


def mcp_access_token(
    conn: psycopg.Connection[Any], manager: KeyManager, row: StoredConnector
) -> str | None:
    """The connector's access token, refreshed and re-sealed first if it is near expiry.

    ``proto/enrich-spike/mcp_token.py`` moved here. The token endpoint, client id and
    resource are read off the row (what discovery and registration recorded), so a refresh
    needs no rediscovery — and a server that rotates no refresh token keeps the old one.
    """
    raw = connectors.load_connector_secret(conn, manager, connector_id=row.id)
    if raw is None:
        return None
    doc = json.loads(raw)
    expires_at = datetime.fromisoformat(doc["expires_at"]) if doc.get("expires_at") else None
    now = datetime.now(UTC)
    if expires_at is None or expires_at - REFRESH_MARGIN > now:
        return str(doc["access_token"])
    if not doc.get("refresh_token"):
        raise RuntimeError(f"connector {row.id}: access token expired and no refresh token")
    if not (row.oauth_token_endpoint and row.oauth_client_id and row.oauth_resource):
        raise RuntimeError(f"connector {row.id}: row lacks token endpoint / client id / resource")
    fresh = _refresh_token_set(
        token_endpoint=row.oauth_token_endpoint,
        client_id=row.oauth_client_id,
        refresh_token=str(doc["refresh_token"]),
        resource=row.oauth_resource,
        now=now,
    )
    if not fresh.get("refresh_token"):
        fresh["refresh_token"] = doc["refresh_token"]
    new_expiry = datetime.fromisoformat(fresh["expires_at"]) if fresh.get("expires_at") else None
    connectors.store_connector_secret(
        conn, manager, connector_id=row.id, secret=json.dumps(fresh), expires_at=new_expiry
    )
    logger.info(
        "enrich: refreshed connector %s token; new expiry %s",
        row.id,
        new_expiry.isoformat() if new_expiry else "none",
    )
    return str(fresh["access_token"])


def _refresh_token_set(
    *, token_endpoint: str, client_id: str, refresh_token: str, resource: str, now: datetime
) -> dict[str, Any]:
    """One ``refresh_token`` grant, as the MCP authorization spec shapes it.

    ``motet_api.mcp_oauth`` holds the same request; the worker cannot import the API (the
    dependency arrow only goes one way), so this is the four lines of it that a refresh
    needs. A follow-up is to move the token client into a package both can reach.
    """
    import httpx  # noqa: PLC0415 — the real runner's path only

    response = httpx.post(
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": resource,
        },
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"the token refresh was refused ({response.status_code})")
    body = response.json()
    access = body.get("access_token")
    if not access:
        raise RuntimeError("the token response carried no access_token")
    expires_in = body.get("expires_in")
    expires_at = (
        (now + timedelta(seconds=int(expires_in))).isoformat()
        if isinstance(expires_in, int | float) and expires_in > 0
        else None
    )
    return {
        "access_token": str(access),
        "refresh_token": str(body["refresh_token"]) if body.get("refresh_token") else None,
        "expires_at": expires_at,
        "token_type": str(body.get("token_type") or "Bearer"),
        "scope": str(body.get("scope") or ""),
    }


# --- redaction --------------------------------------------------------------------------

#: ``proto/enrich-spike/summarize.py``'s rules, plus the ones the harness made necessary.
_REDACT: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\b\d{6}\b"), "<6-digit>"),
    (re.compile(r"\bstrad_[A-Za-z0-9_-]+"), "strad_<redacted>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"), "<email>"),
    # `upn` is SendGrid's per-recipient click-tracking parameter; `eu` is The Information's
    # per-recipient article token — both identify the subscriber.
    (
        re.compile(r"([?&](?:eu|upn|token|code|key|auth|sig|session)=)[A-Za-z0-9_.%-]{8,}"),
        r"\1<redacted>",
    ),
    # A URL whose path carries a long opaque segment — a magic link, a tracking hop.
    (re.compile(r"(https?://[^\s\"'<>]+/)[A-Za-z0-9_-]{24,}(?=[/?#\s\"'<>]|$)"), r"\1<redacted>"),
    (re.compile(r'("value"\s*:\s*")[^"]{8,}(")'), r"\1<redacted>\2"),
)

#: Tools whose *results* are the user's mailbox (or any MCP server): the whole result is
#: replaced, because the login email's body is precisely what must not be stored.
_BROWSER_TOOL_PREFIX: Final = "playwright_"


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def redact_transcript(
    entries: Sequence[Mapping[str, Any]], secrets: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Every string field redacted; every non-browser tool result replaced outright."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        tool = str(item.get("tool") or "")
        if item.get("kind") == "tool_result" and tool and not tool.startswith(_BROWSER_TOOL_PREFIX):
            size = len(str(item.get("result") or ""))
            item["result"] = f"<{size} chars from {tool}, not stored>"
        for key, value in list(item.items()):
            if isinstance(value, str):
                item[key] = redact(value, secrets)
        out.append(item)
    return out


# --- the real runner --------------------------------------------------------------------


def toolchain_dir() -> Path:
    """Where ``node_modules`` with pi, pi-mcp-adapter and the browser server live."""
    configured = os.environ.get(TOOLCHAIN_ENV)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "proto" / "enrich-spike"


class SubprocessPiRunner:
    """Run ``pi -p --mode json`` once, in a private directory, and read the stream back.

    Every run gets a fresh ``0700`` temp directory holding its ``.pi/mcp.json``, its Pi
    home (``PI_CODING_AGENT_DIR``, with the ``models.json`` that teaches Pi's OpenRouter
    catalogue the configured model), the bearer files the ``!command`` hooks read, and the
    storage-state files the browser server reads and writes. The directory is removed in a
    ``finally``. Pi is started in a session of its own so a timeout can kill the whole
    process group — Pi, the adapter's MCP servers, and Chromium under them.
    """

    def __init__(
        self,
        *,
        toolchain: Path | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> None:
        self.toolchain = toolchain or toolchain_dir()
        self.model = model or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
        self.thinking = thinking or os.environ.get(THINKING_ENV, "").strip() or DEFAULT_THINKING

    def run(self, request: EnrichRequest) -> EnrichOutcome:
        pi = self.toolchain / "node_modules" / ".bin" / "pi"
        adapter = self.toolchain / "node_modules" / "pi-mcp-adapter"
        if not pi.exists() or not adapter.exists():
            return EnrichOutcome(
                status="failed",
                error=f"the enrichment toolchain is not installed under {self.toolchain}",
            )
        run_dir = Path(tempfile.mkdtemp(prefix="motet-enrich-"))
        try:
            return self._run_in(run_dir, pi, adapter, request)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def _run_in(
        self, run_dir: Path, pi: Path, adapter: Path, request: EnrichRequest
    ) -> EnrichOutcome:
        state_in = run_dir / "storage-state.in.json"
        state_out = run_dir / "storage-state.out.json"
        if request.storage_state_json:
            _write_private(state_in, request.storage_state_json)
        self._write_mcp_config(run_dir, request)
        home = self._write_pi_home(run_dir)

        env = {
            **os.environ,
            "PI_CODING_AGENT_DIR": str(home),
            TOOLCHAIN_ENV: str(self.toolchain),
            "MOTET_STORAGE_STATE_IN": str(state_in),
            "MOTET_STORAGE_STATE_OUT": str(state_out),
        }
        command = [
            str(pi),
            "-p",
            "--mode",
            "json",
            "-e",
            str(adapter),
            "--no-skills",
            "--no-context-files",
            "--no-builtin-tools",
            "--session",
            str(run_dir / "pi-session.jsonl"),
            "--provider",
            "openrouter",
            "--model",
            self.model,
            "--thinking",
            self.thinking,
            "--system-prompt",
            (HARNESS_DIR / "system-prompt.md").read_text(),
            _prompt(request),
        ]
        stderr_path = run_dir / "stderr.txt"
        started = time.monotonic()
        timed_out = threading.Event()
        with stderr_path.open("w") as stderr:
            process = subprocess.Popen(
                command,
                cwd=run_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
            timer = threading.Timer(request.timeout_seconds, _kill_group, args=(process, timed_out))
            timer.daemon = True
            timer.start()
            try:
                assert process.stdout is not None
                parsed = _parse_stream(process.stdout)
                process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    _kill_group(process, threading.Event())
        elapsed = time.monotonic() - started

        storage_state = state_out.read_text() if state_out.exists() else None
        final = parsed.final_text
        status_line = _field(final, "STATUS")
        logged_in = _field(final, "LOGGED_IN")
        notes = _field(final, "NOTES")
        article = _fence(final, "ARTICLE_MARKDOWN")

        if timed_out.is_set():
            status: RunStatus = "timeout"
            error: str | None = f"the agent did not finish within {request.timeout_seconds}s"
        elif status_line.lower().startswith("ok"):
            status, error = "ok", None
        elif status_line.upper().startswith("BLOCKED"):
            status, error = "blocked", status_line
        else:
            status = "failed"
            tail = redact(_tail(stderr_path), request.secrets)
            error = f"pi exited {process.returncode} without a STATUS line" + (
                f": {tail}" if tail else ""
            )
        logger.info(
            "enrich: pi exited %s after %.0fs with %d tool call(s), $%.4f",
            process.returncode,
            elapsed,
            parsed.tool_calls,
            parsed.cost_usd,
        )
        return EnrichOutcome(
            status=status,
            article_markdown=article,
            logged_in=(logged_in.split()[0].lower() if logged_in else None),
            notes=notes,
            tool_calls=parsed.tool_calls,
            cost_usd=parsed.cost_usd if parsed.saw_usage else None,
            transcript=parsed.entries,
            storage_state_json=storage_state,
            error=error,
        )

    def _write_mcp_config(self, run_dir: Path, request: EnrichRequest) -> None:
        servers: dict[str, Any] = {
            "playwright": {
                "command": "node",
                "args": [str(HARNESS_DIR / "browser-mcp.mjs")],
                "env": {
                    "STEALTH_MODE": "true",
                    "HEADLESS": "true",
                    "NAVIGATION_TIMEOUT": "45000",
                    "TIMEOUT": "90000",
                },
                "inheritEnv": True,
                "lifecycle": "lazy-keep-alive",
                "directTools": ["browser_execute", "browser_screenshot", "browser_get_state"],
            }
        }
        for server in request.mcp_servers:
            bearer_file = run_dir / f"{server.name}.bearer"
            _write_private(bearer_file, server.bearer)
            hook = run_dir / f"bearer-{server.name}.sh"
            _write_private(
                hook,
                f'#!/bin/sh\nprintf \'Bearer %s\' "$(cat "$(dirname "$0")/{bearer_file.name}")"\n',
                mode=0o700,
            )
            servers[server.name] = {
                "url": server.url,
                "auth": "bearer",
                "headers": {"Authorization": f"!./{hook.name}"},
                "lifecycle": "lazy",
                "directTools": True,
            }
        config = {
            "settings": {"toolPrefix": "server", "requestTimeoutMs": 120000, "idleTimeout": 600},
            "mcpServers": servers,
        }
        (run_dir / ".pi").mkdir(mode=0o700)
        _write_private(run_dir / ".pi" / "mcp.json", json.dumps(config, indent=2))

    def _write_pi_home(self, run_dir: Path) -> Path:
        home = run_dir / "pi-home"
        home.mkdir(mode=0o700)
        spec = KNOWN_MODELS.get(self.model)
        models = {
            "providers": {
                "openrouter": {
                    "models": [
                        {
                            "id": self.model,
                            "name": f"{self.model} (OpenRouter)",
                            "reasoning": True,
                            "input": ["text", "image"],
                            "cost": {
                                "input": spec.input_usd_per_mtok if spec else 3,
                                "output": spec.output_usd_per_mtok if spec else 15,
                                "cacheRead": spec.cache_read_usd_per_mtok if spec else 0.3,
                                "cacheWrite": spec.cache_write_usd_per_mtok if spec else 3.75,
                            },
                            "contextWindow": spec.context_tokens if spec else 200_000,
                            "maxTokens": min(spec.max_output_tokens, 64_000) if spec else 32_000,
                        }
                    ]
                }
            }
        }
        _write_private(home / "models.json", json.dumps(models))
        _write_private(
            home / "settings.json",
            json.dumps(
                {"defaultProvider": "openrouter", "defaultModel": self.model, "quietStartup": True}
            ),
        )
        _write_private(home / "auth.json", "{}")
        return home


def _prompt(request: EnrichRequest) -> str:
    lines = [f"ARTICLE_URL: {request.article_url}", f"SITE_DOMAIN: {request.domain}"]
    if request.login_email:
        lines.append(f"LOGIN_EMAIL: {request.login_email}")
        lines.append(
            f"LOGIN_PASSWORD: {request.login_password}"
            if request.login_password
            else "LOGIN_PASSWORD: none — this site uses a passwordless login (emailed code or "
            "magic link)"
        )
    else:
        lines.append(
            "LOGIN_EMAIL: none — no credential is on file for this site; fetch what is "
            "readable without logging in, and report BLOCKED: no-credential if it is walled"
        )
    if request.mcp_servers:
        lines.append("MCP SERVERS:")
        lines.extend(f"- {s.name}: {s.label}" for s in request.mcp_servers)
    else:
        lines.append("MCP SERVERS: none (no mailbox tools; a magic-link login cannot be completed)")
    lines.append(
        "BROWSER: cookies from an earlier session on this site are already loaded"
        if request.storage_state_json
        else "BROWSER: fresh session, no saved cookies"
    )
    lines.append("Go.")
    return "\n".join(lines)


@dataclass
class _Parsed:
    entries: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    cost_usd: float = 0.0
    saw_usage: bool = False
    final_text: str = ""


def _parse_stream(stream: Any) -> _Parsed:
    """Read Pi's JSON-lines event stream as it arrives, keeping the parts worth storing."""
    parsed = _Parsed()
    seq = 0
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        at = datetime.now(UTC).isoformat(timespec="seconds")
        if kind == "tool_execution_start":
            parsed.tool_calls += 1
            seq += 1
            parsed.entries.append(
                {
                    "seq": seq,
                    "at": at,
                    "kind": "tool_call",
                    "tool": str(event.get("toolName") or ""),
                    "args": _compact(event.get("args"), TRANSCRIPT_FIELD_CHARS),
                }
            )
        elif kind == "tool_execution_end":
            seq += 1
            parsed.entries.append(
                {
                    "seq": seq,
                    "at": at,
                    "kind": "tool_result",
                    "tool": str(event.get("toolName") or ""),
                    "ok": not event.get("isError"),
                    "result": _compact(_result_text(event.get("result")), TRANSCRIPT_FIELD_CHARS),
                }
            )
        elif kind == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            message = event["message"]
            text = "\n".join(
                str(part.get("text") or "")
                for part in message.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            usage = message.get("usage") or {}
            cost = (usage.get("cost") or {}).get("total")
            if isinstance(cost, int | float):
                parsed.cost_usd += float(cost)
                parsed.saw_usage = True
            if not text:
                # A turn that only issued tool calls. Its cost is in the total; a blank
                # line in the transcript would say nothing a reviewer can use.
                continue
            parsed.final_text = text
            seq += 1
            parsed.entries.append(
                {
                    "seq": seq,
                    "at": at,
                    "kind": "assistant",
                    "text": text[:ASSISTANT_TEXT_CHARS],
                    "cost_usd": float(cost) if isinstance(cost, int | float) else None,
                    "tokens": {
                        k: usage.get(k) for k in ("input", "output", "cacheRead", "cacheWrite")
                    },
                }
            )
    return parsed


def _result_text(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return "\n".join(
            str(part.get("text") or "")
            for part in result["content"]
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return result if isinstance(result, str) else json.dumps(result)


def _compact(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f"… [{len(text)} chars]"


def _field(final: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.*)$", final, re.M)
    return match.group(1).strip() if match else ""


def _fence(final: str, tag: str) -> str:
    match = re.search(r"```" + tag + r"\n(.*?)```", final, re.S)
    return match.group(1).strip() if match else ""


def _tail(path: Path, chars: int = 400) -> str:
    try:
        return path.read_text()[-chars:].strip()
    except OSError:
        return ""


def _kill_group(process: subprocess.Popen[str], flag: threading.Event) -> None:
    flag.set()
    try:
        os.killpg(process.pid, 15)
        time.sleep(3)
        if process.poll() is None:
            os.killpg(process.pid, 9)
    except ProcessLookupError:
        pass


def _write_private(path: Path, content: str, *, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _permanent(message: str) -> Exception:
    from .handlers import PermanentFailure  # noqa: PLC0415

    return PermanentFailure(message)


__all__ = [
    "ENRICH_TIMEOUT_SECONDS",
    "EnrichOutcome",
    "EnrichRequest",
    "FakePiRunner",
    "McpServerSpec",
    "PiRunner",
    "SubprocessPiRunner",
    "build_pi_runner",
    "enrich_failed",
    "handle_enrich",
    "matching_site",
    "mcp_access_token",
    "redact",
    "redact_transcript",
]
