"""Bring the whole local stack up with one command, and take it down with one Ctrl-C.

``bin/dev`` is the process half of local setup; ``docker-compose.yml`` is the stateful
half and ``bin/local-env`` is the secrets half (motet#79, motet#80). Together they are the
acceptance test motet#83 states: **from a fresh clone, a working laptop is ``bin/local-env``
plus one more command.**

What it does, in order: brings the compose Postgres up and waits for it to be *healthy*
rather than merely running, applies migrations against it, then starts three long-lived
processes — the API, a polling worker, and the SPA's dev server — into one prefixed log
stream, and supervises them.

Four things about the supervision are the design rather than the implementation:

* **Every child gets a session of its own** (``start_new_session=True``), and teardown
  signals the *process group*. Both of the interesting children are wrappers — ``uv run``
  around ``uvicorn``, ``npm run dev`` around ``vite`` — so signalling the pid we hold
  reaches the wrapper and orphans the process actually holding the port. A group signal is
  the difference between "Ctrl-C worked" and "port 8000 is still busy, and nothing says
  why". ``tools/tests/test_dev.py`` proves it on a real grandchild rather than asserting a
  call was made.
* **The supervisor traps SIGINT rather than sharing it.** A new session means Ctrl-C at
  the terminal reaches this process and nothing else, so teardown happens in one place, in
  a known order, with a grace period and a SIGKILL behind it — instead of three children
  racing a signal they each handle differently.
* **One child exiting brings the rest down.** A worker that failed its startup validation
  leaves an API and a dev server running against a queue nobody drains, which is
  motet#38's failure mode wearing a laptop's clothes: the SPA looks fine and nothing
  happens. The line naming which process exited, and with what status, is the whole
  report.
* **A ``.env`` is read only when ``UV_ENV_FILE`` says so.** ``uv run`` does not read one on
  its own, and this script does not make it: real mode spends real money against staging's
  caps (CONTRIBUTING.md), so it stays the deliberate act it is documented as. What this
  script does do is *say* the file is there and unread, because "my .env is ignored" and
  "I forgot the export" are the same five minutes.

Run it through ``bin/dev``; see CONTRIBUTING.md for the loop it belongs to.
"""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

#: Where the API listens, and the one number that has to reach two places. The Vite dev
#: server proxies ``/v1`` to it (``web/vite.config.ts``); before motet#83 that target was
#: a literal in the Vite config and this was a literal in a documented command line, so
#: moving the API off 8000 made ``/v1/...`` return ``index.html`` and fail as a JSON parse
#: error pointing nowhere near the cause. Now it is set here and *passed* to Vite through
#: :data:`API_PORT_ENV`.
DEFAULT_API_PORT = 8000

#: Vite's own default. Pinned rather than left to Vite because a busy 5173 makes Vite pick
#: 5174 with a one-line notice, and a moved SPA port breaks both OAuth flows — Google
#: matches a registered redirect URI as an exact string. ``--strictPort`` below turns that
#: into a refusal instead.
DEFAULT_WEB_PORT = 5173

#: What ``web/vite.config.ts`` reads to find the API. Keep the two in step.
API_PORT_ENV = "MOTET_DEV_API_PORT"

#: Where ``--voice`` runs the voice service. The prototype's hand-launched port.
DEFAULT_VOICE_PORT = 8100

#: A local worker polls rather than draining once and exiting: ``MOTET_DRAIN_TRIGGER`` is
#: deliberately unset on a laptop (there is no Cloud Run job to nudge), so nothing else
#: would start one.
DEFAULT_POLL_SECONDS = 2

#: The database a local run uses when nothing says otherwise — the same string
#: ``.env.example`` carries and ``bin/local-env`` writes, pointed at the compose service.
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/motet_dev"

#: The compose service to wait on. Everything else in the file is scaffolding for it.
DATABASE_SERVICE = "postgres"

#: How long a child gets to exit on SIGTERM before its group is killed. Generous enough
#: for the worker, which finishes the job it is holding before it stops.
SHUTDOWN_GRACE_SECONDS = 10.0

#: ANSI colours for the log prefixes, used only when stdout is a terminal.
_COLOURS = {"api": "36", "worker": "35", "web": "32", "voice": "33", "dev": "1"}
_RESET = "\033[0m"


class DevError(Exception):
    """Something a developer can fix, reported as a sentence rather than a traceback."""


@dataclass(frozen=True)
class Service:
    """One long-lived child process and how its output is labelled."""

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    #: Ports this service needs free before it starts. Checked up front so that a busy
    #: port is one clear line rather than three processes and one confusing traceback.
    ports: tuple[int, ...] = ()
    #: Where the child runs. Pinned rather than inherited, so that `uvicorn --reload`
    #: watches the repo whichever directory `python -m tools.dev` was invoked from.
    cwd: Path | None = None


def _paint(name: str, text: str, colour: bool) -> str:
    if not colour:
        return f"[{name}] {text}"
    return f"\033[{_COLOURS.get(name, '0')}m[{name}]{_RESET} {text}"


class Supervisor:
    """Run several processes as one, and stop them as one.

    Split from :func:`main` so that the property that matters — teardown leaves nothing
    behind — is testable without a database, a port, or a toolchain.
    """

    def __init__(
        self,
        services: Sequence[Service],
        *,
        out: IO[str] | None = None,
        colour: bool | None = None,
        grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.services = tuple(services)
        self._out = out if out is not None else sys.stdout
        self._colour = self._out.isatty() if colour is None else colour
        self._grace = grace_seconds
        self._procs: dict[str, subprocess.Popen[str]] = {}
        #: The process group each child leads, recorded when it is spawned rather than
        #: derived at teardown. `os.getpgid(pid)` raises once the child has been *reaped*,
        #: so a wrapper that exits before the process it spawned would leave the group
        #: unsignalled — which is exactly the orphan this whole mechanism is about.
        #: With ``start_new_session=True`` the group id is the child's own pid, and the
        #: kernel keeps that pid number reserved while the group still has members.
        self._groups: dict[str, int] = {}
        self._readers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    # -- output ------------------------------------------------------------------

    def say(self, name: str, text: str) -> None:
        with self._lock:
            self._out.write(_paint(name, text, self._colour) + "\n")
            self._out.flush()

    def _pump(self, name: str, stream: IO[str]) -> None:
        with stream:
            for line in stream:
                self.say(name, line.rstrip("\n"))

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        for service in self.services:
            env = {**os.environ, **service.env}
            # shlex.join rather than a plain join: an argument carrying a space — or a
            # newline — has to read back as the one argument it is.
            self.say("dev", f"starting {service.name}: {shlex.join(service.argv)}")
            try:
                proc = subprocess.Popen(
                    service.argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    cwd=service.cwd,
                    # The whole teardown story. See the module docstring.
                    start_new_session=True,
                )
            except OSError as exc:
                self.shutdown()
                raise DevError(f"could not start {service.name}: {exc}") from exc
            self._procs[service.name] = proc
            self._groups[service.name] = proc.pid
            stream = proc.stdout
            if stream is None:  # pragma: no cover — stdout=PIPE guarantees one
                raise DevError(f"{service.name} was started without a readable stdout")
            reader = threading.Thread(target=self._pump, args=(service.name, stream), daemon=True)
            reader.start()
            self._readers.append(reader)

    def poll(self) -> tuple[str, int] | None:
        """Return the first child to have exited, as ``(name, status)``."""
        for name, proc in self._procs.items():
            status = proc.poll()
            if status is not None:
                return name, status
        return None

    def shutdown(self, sig: int = signal.SIGTERM) -> None:
        """Signal every child's process group, then kill whatever is still there.

        Signalling the group rather than the pid is what reaches ``uvicorn`` behind
        ``uv run`` and ``vite`` behind ``npm run dev``. A process that has already exited
        leaves no group, which is a ``ProcessLookupError`` and not a problem.
        """
        if self._stopping.is_set():
            return
        self._stopping.set()
        for name in self._procs:
            # Signalled whether or not the child we hold is still alive: a wrapper can
            # exit while the process it spawned is still running and still holding a
            # port, and that survivor is in this group.
            self.say("dev", f"stopping {name}")
            _signal_group(self._groups[name], sig)
        deadline = time.monotonic() + self._grace
        for proc in self._procs.values():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        for name, proc in self._procs.items():
            if proc.poll() is None:
                self.say("dev", f"{name} did not stop in time; killing its process group")
            # Unconditional, and that is deliberate: a wrapper can exit while the process
            # it spawned holds the port, so the group is killed even when the child we
            # hold is already reaped. There is nothing left to kill in the common case.
            _signal_group(self._groups[name], signal.SIGKILL)
            # Reaped here rather than left to the interpreter: a killed child that nobody
            # waits on is a zombie, and this process may go on to start another stack.
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover — SIGKILL is not refusable
                pass
        for reader in self._readers:
            reader.join(timeout=1.0)

    def run(self) -> int:
        """Start everything, then wait for a signal or for a child to exit."""
        stop = threading.Event()
        caught: list[int] = []

        def _handle(signum: int, _frame: object) -> None:
            caught.append(signum)
            stop.set()

        previous = [(sig, signal.signal(sig, _handle)) for sig in (signal.SIGINT, signal.SIGTERM)]
        try:
            self.start()
            while not stop.is_set():
                ended = self.poll()
                if ended is not None:
                    name, status = ended
                    self.say("dev", f"{name} exited with status {status}; stopping the rest")
                    self.shutdown()
                    # Popen reports a signalled child as a negative number; a shell reports
                    # it as 128+n, and this script is run from one.
                    return status if status >= 0 else 128 - status
                time.sleep(0.2)
            signum = caught[0] if caught else int(signal.SIGINT)
            self.say("dev", f"got {signal.Signals(signum).name}; shutting down")
            self.shutdown()
            return 128 + signum
        finally:
            for sig, handler in previous:
                signal.signal(sig, handler)


def _signal_group(pgid: int, sig: int) -> None:
    """Signal a process group by the id recorded when its leader was spawned.

    Deliberately **not** ``os.killpg(os.getpgid(pid), sig)``: ``getpgid`` needs a live
    pid-table entry, and the leader is reaped as soon as it exits — so deriving the group
    at teardown means the one case that matters, a wrapper that dies before the process
    it spawned, silently signals nothing. An empty group is a ``ProcessLookupError`` and
    is the ordinary outcome.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# -- environment ------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the ``KEY=value`` lines of a ``.env`` well enough to read its names and values.

    Deliberately small: this exists so the supervisor can resolve ``DATABASE_URL`` for its
    *own* use — waiting on the database, and telling you what it migrated — and so it can
    say which of the file's names an exported variable will beat. Every child
    goes through ``uv run``, which reads the file itself, so nothing here has to be a
    complete dotenv implementation.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = _unescape(value)
        values[name] = value
    return values


#: The escapes ``bin/local-env``'s ``_render_value`` writes inside double quotes, which is
#: the subset of uv's that a file this repo writes can contain.
_ESCAPES = {"\\": "\\", '"': '"', "$": "$", "n": "\n"}


def _unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value) and value[i + 1] in _ESCAPES:
            out.append(_ESCAPES[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def read_env_files(paths: Iterable[Path]) -> dict[str, str]:
    """Several env files merged the way uv merges them: a later file wins."""
    merged: dict[str, str] = {}
    for path in paths:
        merged.update(read_env_file(path))
    return merged


def env_file_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    """The ``.env`` files ``uv run`` will read, in order, or empty if it will read none.

    ``UV_ENV_FILE`` is the only thing that turns one on, and this script does not set it —
    see the module docstring for why that is a decision rather than an omission. It takes
    a **whitespace-separated list**, so a single ``Path(named)`` would turn two files into
    one path that does not exist, and read nothing while reporting nothing.
    """
    env = os.environ if environ is None else environ
    named = env.get("UV_ENV_FILE")
    if not named or env.get("UV_NO_ENV_FILE"):
        return []
    return [Path(part) for part in named.split()]


def resolve_database_url(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """What the children will connect to, and *where that came from*.

    Resolved the way ``uv run`` resolves it: an exported variable wins over the env file —
    that is uv's precedence, not a choice made here — and the fallback is the compose
    service, which is the same string ``.env.example`` carries.

    The source is returned because it decides whether this value is *injected* into the
    children. Only ``"default"`` is: in the other two cases the children resolve the same
    value themselves, and overriding uv's own answer with our re-reading of it would turn
    every gap between the two — ``${VAR}`` interpolation, which uv does and
    :func:`read_env_file` does not — into a laptop quietly pointed at a different
    database.
    """
    env = os.environ if environ is None else environ
    exported = env.get("DATABASE_URL")
    if exported:
        return exported, "environment"
    from_file = read_env_files(env_file_paths(env)).get("DATABASE_URL")
    if from_file:
        return from_file, "env file"
    return DEFAULT_DATABASE_URL, "default"


def shadowed_names(paths: Iterable[Path], environ: Mapping[str, str] | None = None) -> list[str]:
    """The names an env file sets that this shell already exports **with a different value**.

    ``uv run --env-file`` never overrides a variable that is already in the environment —
    uv's precedence, the same one :func:`resolve_database_url` follows — so each of these
    is a line of the file that silently does nothing. That is motet#85: a stale
    ``OPENROUTER_API_KEY`` exported from ``~/.zshrc`` beat the one ``bin/local-env`` wrote,
    every health field said real mode was armed, and every ``integrate`` job failed with a
    vendor 401 pointing nowhere near the cause.

    **Returns names only.** Values are compared and never returned, because they are live
    credentials and the caller prints the result. A name exported with the *same* value is
    left out: it changes nothing that runs, and ``set -a; . ./.env`` is an ordinary way to
    have one. Where :func:`read_env_file`'s small parser reads a value differently from
    uv's — ``${VAR}`` interpolation — the comparison errs toward reporting, which costs a
    line rather than hiding a key.

    **Only meaningful in a process uv did not fill from the same file**, which is why
    ``bin/dev`` and ``bin/local-env`` run under ``uv run --no-env-file``: otherwise every
    line of the file is "exported" by the time this reads the environment.
    """
    env = os.environ if environ is None else environ
    return sorted(
        name for name, value in read_env_files(paths).items() if name in env and env[name] != value
    )


def shadow_warning(names: Sequence[str], files: str) -> str:
    """The sentence both ``bin/dev`` and ``bin/local-env`` print for :func:`shadowed_names`."""
    return (
        f"these names are already exported in this shell and will take precedence over "
        f"{files}: {', '.join(names)}. `uv run` never overrides an exported variable — "
        "`unset` them for the file's values to apply."
    )


# -- the steps --------------------------------------------------------------------


def compose_up(root: Path) -> None:
    """Bring the stateful dependency up and wait for it to be *healthy*.

    ``--wait`` is what makes this a bring-up rather than a race: the compose healthcheck
    asserts both databases answer a query, so migrations that run after it cannot meet a
    server that is still executing its init scripts.
    """
    if shutil.which("docker") is None:
        raise DevError(
            "docker is not on PATH. Start a Postgres yourself and pass --no-db, or see "
            "CONTRIBUTING.md."
        )
    result = _run(
        ["docker", "compose", "up", "-d", "--wait", DATABASE_SERVICE],
        cwd=root,
    )
    if result != 0:
        raise DevError(
            "`docker compose up -d --wait` failed. If a Postgres from the old manual "
            "instructions is still bound to 5432, `docker rm -f motet-pg` and try again."
        )


def ensure_web_dependencies(root: Path) -> bool:
    """Install the SPA's dependencies if they are not there, and say so if it does.

    The Python half needs no equivalent — every child goes through ``uv run``, which syncs
    the workspace itself. ``npm`` has no such behaviour, so without this the one command a
    fresh clone is supposed to need ends in `vite: not found`. Only when
    ``web/node_modules`` is absent: `npm ci` deletes and reinstalls the tree, which is not
    something to do on every `bin/dev`.
    """
    if (root / "web" / "node_modules").exists():
        return False
    if _run(["npm", "--prefix", str(root / "web"), "ci"], cwd=root) != 0:
        raise DevError("`npm ci` failed; the SPA cannot start without its dependencies")
    return True


def migrate(root: Path, database_url: str, *, inject_database_url: bool = True) -> None:
    """Apply migrations against the database the services are about to use.

    Passed explicitly rather than left to be inherited: with no ``.env`` and nothing
    exported there is no ``DATABASE_URL`` anywhere, and the migrate CLI's answer to that
    is a usage message — a laptop's first `bin/dev` would fail on the default database it
    just created.

    Through the environment rather than ``--database-url``, because an argument is in
    every ``ps`` on the machine and a connection string carries a password.

    ``inject_database_url`` is :func:`build_services`' rule, for its reason: when the URL
    came from the env file, the migrate step's own ``uv run`` reads it, and an injected
    copy of our re-reading would override uv's answer.
    """
    env = {**os.environ, **({"DATABASE_URL": database_url} if inject_database_url else {})}
    if _run(["uv", "run", "python", "-m", "motet_db.migrate"], cwd=root, env=env) != 0:
        raise DevError(f"migrations failed against {_redacted(database_url)}")


def _run(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> int:
    """Run a one-shot step, and turn "that program is not installed" into a sentence.

    :class:`DevError`'s docstring promises a sentence rather than a traceback, and
    ``main`` catches nothing else — so an absent ``docker``, ``npm`` or ``uv`` has to be
    converted here rather than escaping as ``FileNotFoundError``.
    """
    try:
        return subprocess.run(
            list(argv), cwd=cwd, check=False, env=None if env is None else dict(env)
        ).returncode
    except OSError as exc:
        raise DevError(f"could not run `{shlex.join(argv)}`: {exc}") from exc


def _redacted(url: str) -> str:
    """A connection string with its password removed, for a line a human reads."""
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    # rpartition, not partition: the separator is the LAST '@', and a password may
    # contain one. Splitting at the first would print most of it.
    creds, _, host = rest.rpartition("@")
    user, has_password, _ = creds.partition(":")
    return f"{scheme}://{user}{':***' if has_password else ''}@{host}"


def port_is_free(port: int) -> bool:
    """Whether a port is free on **both** loopback families.

    Vite is told to bind ``localhost``, which on a dual-stack machine may resolve to
    ``::1`` first — so an IPv4-only probe passes and Vite then dies on ``EADDRINUSE``,
    which is the confusing failure this check exists to replace.
    """
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:  # pragma: no cover — a host without IPv6 at all
            continue
        with probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                    continue  # pragma: no cover — this family is not configured
                return False
    return True


def check_ports(services: Iterable[Service]) -> None:
    busy = [
        (service.name, port)
        for service in services
        for port in service.ports
        if not port_is_free(port)
    ]
    if busy:
        listed = ", ".join(f"{port} ({name})" for name, port in busy)
        raise DevError(
            f"port already in use: {listed}. Stop whatever holds it, or pass --api-port / "
            "--web-port."
        )


def build_services(
    *,
    root: Path,
    api_port: int,
    web_port: int,
    poll_seconds: int,
    database_url: str = DEFAULT_DATABASE_URL,
    inject_database_url: bool = True,
    without: Sequence[str] = (),
    voice_port: int | None = None,
    voice_start_token: str = "",
) -> list[Service]:
    """The three processes, and the one place the API's port is written down.

    The web service is handed :data:`API_PORT_ENV` rather than trusting it to be exported:
    the whole point is that one number reaches both the server that listens on it and the
    proxy that forwards to it.

    ``DATABASE_URL`` is handed to the two Python processes **only when nothing else would
    give them one** — ``inject_database_url``, which ``main`` sets from
    :func:`resolve_database_url`'s source. The case it covers is a fresh clone with no
    ``.env`` and nothing exported, where there is no ``DATABASE_URL`` anywhere and the
    migrate CLI answers with a usage message. In the other two cases the children resolve
    it themselves, through the same ``uv run`` that read it, and an injected copy would
    *override* uv's own answer with our re-reading of it.

    ``voice_port`` adds the voice service (``--voice``, off by default) and points the API
    at it with one start token both sides are handed — the same shape a deployment has, so
    Play Live runs through the API locally exactly as it would deployed. The voice service
    takes ``MOTET_VOICE_ARM`` and the inference mode from the environment like everything
    else here; nothing about it is decided by this function.
    """
    database_env = {"DATABASE_URL": database_url} if inject_database_url else {}
    voice_env = (
        {
            "MOTET_VOICE_BASE_URL": f"http://localhost:{voice_port}",
            "MOTET_VOICE_START_SESSION_TOKEN": voice_start_token,
        }
        if voice_port is not None
        else {}
    )
    services = [
        Service(
            name="api",
            argv=(
                "uv",
                "run",
                "uvicorn",
                "motet_api:app",
                "--reload",
                "--port",
                str(api_port),
            ),
            env={**database_env, **voice_env},
            ports=(api_port,),
            cwd=root,
        ),
        Service(
            name="worker",
            argv=(
                "uv",
                "run",
                "python",
                "-m",
                "motet_workers.runner",
                "all",
                "--poll-seconds",
                str(poll_seconds),
            ),
            env=database_env,
            cwd=root,
        ),
        Service(
            name="web",
            argv=(
                "npm",
                "--prefix",
                str(root / "web"),
                "run",
                "dev",
                "--",
                "--port",
                str(web_port),
                "--strictPort",
            ),
            env={API_PORT_ENV: str(api_port)},
            ports=(web_port,),
            cwd=root,
        ),
    ]
    if voice_port is not None:
        services.append(
            Service(
                name="voice",
                argv=(
                    "uv",
                    "run",
                    "uvicorn",
                    "motet_voice.app:create_app",
                    "--factory",
                    "--reload",
                    "--port",
                    str(voice_port),
                ),
                env={
                    "MOTET_VOICE_START_SESSION_TOKEN": voice_start_token,
                    "MOTET_VOICE_ALLOWED_ORIGINS": f"http://localhost:{web_port}",
                },
                ports=(voice_port,),
                cwd=root,
            )
        )
    return [service for service in services if service.name not in without]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bin/dev",
        description="Run the local Motet stack: Postgres, migrations, API, worker, SPA.",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=int(os.environ.get(API_PORT_ENV, DEFAULT_API_PORT)),
        help=(
            f"port for the API, and what the Vite proxy targets (default {DEFAULT_API_PORT}). "
            "It does NOT move MOTET_PUBLIC_BASE_URL, which bin/local-env writes as a "
            "literal, so real-mode feed and audio URLs still name 8000."
        ),
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=(
            f"port for the SPA dev server (default {DEFAULT_WEB_PORT}; moving it breaks "
            "the registered OAuth redirect URIs)"
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help=f"how often the worker sweeps the queues (default {DEFAULT_POLL_SECONDS})",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="do not touch docker compose; use the Postgres DATABASE_URL already names",
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        help="skip applying migrations before starting anything",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help=(
            "also run the voice service and point the API at it, so Play Live works "
            "(off by default; MOTET_VOICE_ARM picks the arm, and the realtime arm is billed "
            "per audio token in real mode)"
        ),
    )
    parser.add_argument(
        "--voice-port",
        type=int,
        default=DEFAULT_VOICE_PORT,
        help=f"port for the voice service with --voice (default {DEFAULT_VOICE_PORT})",
    )
    parser.add_argument(
        "--without",
        action="append",
        choices=["api", "worker", "web"],
        default=[],
        metavar="NAME",
        help="do not start this process (repeatable): api, worker, web",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    out = sys.stdout
    colour = out.isatty()

    def say(text: str) -> None:
        out.write(_paint("dev", text, colour) + "\n")
        out.flush()

    try:
        env_files = env_file_paths()
        if env_files:
            named = ", ".join(str(path) for path in env_files)
            say(f"reading {named} (UV_ENV_FILE) — this run uses whatever mode it sets")
        elif (root / ".env").exists():
            say(
                ".env is present but NOT being read: `uv run` needs UV_ENV_FILE. "
                "`export UV_ENV_FILE=.env` for real mode (it spends money — CONTRIBUTING.md)."
            )

        database_url, source = resolve_database_url()
        # DATABASE_URL already gets a line of its own naming where it came from, so a
        # collision on it is said there rather than twice. Only when that line says
        # "environment": an exported-but-empty one is a collision the label cannot show.
        label = source
        shadowed = shadowed_names(env_files)
        if source == "environment" and "DATABASE_URL" in shadowed:
            shadowed.remove("DATABASE_URL")
            label = "environment, over the env file's DATABASE_URL"
        if shadowed:
            say("warning: " + shadow_warning(shadowed, ", ".join(map(str, env_files))))

        services = build_services(
            root=root,
            api_port=args.api_port,
            web_port=args.web_port,
            poll_seconds=args.poll_seconds,
            database_url=database_url,
            inject_database_url=source == "default",
            without=args.without,
            voice_port=args.voice_port if args.voice else None,
            # Minted per run: it only has to agree between two children of this process.
            voice_start_token=secrets.token_urlsafe(24) if args.voice else "",
        )
        check_ports(services)

        if args.no_db:
            say(f"--no-db: using {_redacted(database_url)} ({label}) as it is")
        else:
            say("bringing up Postgres (docker compose up -d --wait)")
            compose_up(root)
            say(f"Postgres is healthy: {_redacted(database_url)} ({label})")

        if any(service.name == "web" for service in services):
            say("checking the SPA's dependencies")
            if ensure_web_dependencies(root):
                say("installed web/node_modules")

        if args.no_migrate:
            say("--no-migrate: skipping migrations")
        else:
            say("applying migrations")
            migrate(root, database_url, inject_database_url=source == "default")

        if not services:
            say("nothing left to start (--without)")
            return 0
        voice = f", voice on http://localhost:{args.voice_port}" if args.voice else ""
        say(
            f"API on http://localhost:{args.api_port}, SPA on http://localhost:{args.web_port}"
            f"{voice} — Ctrl-C stops all of it"
        )
        return Supervisor(services, out=out, colour=colour).run()
    except DevError as exc:
        say(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
