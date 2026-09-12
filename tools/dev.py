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
import os
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
_COLOURS = {"api": "36", "worker": "35", "web": "32", "dev": "1"}
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
                    # The whole teardown story. See the module docstring.
                    start_new_session=True,
                )
            except OSError as exc:
                self.shutdown()
                raise DevError(f"could not start {service.name}: {exc}") from exc
            self._procs[service.name] = proc
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
        for name, proc in self._procs.items():
            if proc.poll() is not None:
                continue
            self.say("dev", f"stopping {name}")
            _signal_group(proc.pid, sig)
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
            _signal_group(proc.pid, signal.SIGKILL)
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


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


# -- environment ------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the ``KEY=value`` lines of a ``.env`` well enough to read one variable.

    Deliberately small: this exists so the supervisor can resolve ``DATABASE_URL`` for its
    *own* use — waiting on the database, and telling you what it migrated. Every child
    goes through ``uv run``, which reads the file itself, so nothing here has to be a
    complete dotenv implementation.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def env_file_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """The ``.env`` ``uv run`` will read, or ``None`` if it will read none.

    ``UV_ENV_FILE`` is the only thing that turns one on, and this script does not set it —
    see the module docstring for why that is a decision rather than an omission.
    """
    env = os.environ if environ is None else environ
    named = env.get("UV_ENV_FILE")
    if not named or env.get("UV_NO_ENV_FILE"):
        return None
    return Path(named)


def resolve_database_url(environ: Mapping[str, str] | None = None) -> str:
    """What the children will actually connect to, resolved the way ``uv run`` resolves it.

    An exported variable wins over the env file — that is uv's precedence, not a choice
    made here — and the default below is the compose service, which is the same string
    ``.env.example`` carries.
    """
    env = os.environ if environ is None else environ
    exported = env.get("DATABASE_URL")
    if exported:
        return exported
    path = env_file_path(env)
    if path is not None:
        from_file = read_env_file(path).get("DATABASE_URL")
        if from_file:
            return from_file
    return DEFAULT_DATABASE_URL


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
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", DATABASE_SERVICE],
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
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
    result = subprocess.run(["npm", "--prefix", str(root / "web"), "ci"], cwd=root, check=False)
    if result.returncode != 0:
        raise DevError("`npm ci` failed; the SPA cannot start without its dependencies")
    return True


def migrate(root: Path, database_url: str) -> None:
    """Apply migrations against the database the services are about to use.

    Passed explicitly rather than left to be inherited: with no ``.env`` and nothing
    exported there is no ``DATABASE_URL`` anywhere, and the migrate CLI's answer to that
    is a usage message — a laptop's first `bin/dev` would fail on the default database it
    just created.

    Through the environment rather than ``--database-url``, because an argument is in
    every ``ps`` on the machine and a connection string carries a password.
    """
    result = subprocess.run(
        ["uv", "run", "python", "-m", "motet_db.migrate"],
        cwd=root,
        check=False,
        env={**os.environ, "DATABASE_URL": database_url},
    )
    if result.returncode != 0:
        raise DevError(f"migrations failed against {_redacted(database_url)}")


def _redacted(url: str) -> str:
    """A connection string with its password removed, for a line a human reads."""
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    creds, _, host = rest.partition("@")
    user, has_password, _ = creds.partition(":")
    return f"{scheme}://{user}{':***' if has_password else ''}@{host}"


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
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
    without: Sequence[str] = (),
) -> list[Service]:
    """The three processes, and the one place the API's port is written down.

    The web service is handed :data:`API_PORT_ENV` rather than trusting it to be exported:
    the whole point is that one number reaches both the server that listens on it and the
    proxy that forwards to it.

    ``DATABASE_URL`` is handed to the two Python processes for the same reason, and
    handing them the *resolved* value changes nothing when it was already resolved from
    their own environment or from the ``.env`` ``uv run`` reads — :func:`resolve_database_url`
    reads those in uv's own order. What it adds is the case where neither exists, which is
    a fresh clone.
    """
    database_env = {"DATABASE_URL": database_url}
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
            env=database_env,
            ports=(api_port,),
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
        ),
    ]
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
        help=f"port for the API, and what the Vite proxy targets (default {DEFAULT_API_PORT})",
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
        env_file = env_file_path()
        if env_file is not None:
            say(f"reading {env_file} (UV_ENV_FILE) — this run uses whatever mode it sets")
        elif (root / ".env").exists():
            say(
                ".env is present but NOT being read: `uv run` needs UV_ENV_FILE. "
                "`export UV_ENV_FILE=.env` for real mode (it spends money — CONTRIBUTING.md)."
            )

        database_url = resolve_database_url()
        services = build_services(
            root=root,
            api_port=args.api_port,
            web_port=args.web_port,
            poll_seconds=args.poll_seconds,
            database_url=database_url,
            without=args.without,
        )
        check_ports(services)

        if args.no_db:
            say(f"--no-db: using {_redacted(database_url)} as it is")
        else:
            say("bringing up Postgres (docker compose up -d --wait)")
            compose_up(root)
            say(f"Postgres is healthy: {_redacted(database_url)}")

        if any(service.name == "web" for service in services):
            say("checking the SPA's dependencies")
            if ensure_web_dependencies(root):
                say("installed web/node_modules")

        if args.no_migrate:
            say("--no-migrate: skipping migrations")
        else:
            say("applying migrations")
            migrate(root, database_url)

        if not services:
            say("nothing left to start (--without)")
            return 0
        say(
            f"API on http://localhost:{args.api_port}, SPA on http://localhost:{args.web_port}"
            " — Ctrl-C stops all of it"
        )
        return Supervisor(services, out=out, colour=colour).run()
    except DevError as exc:
        say(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
