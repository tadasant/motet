"""What ``bin/dev`` starts, what it refuses, and — above all — what it leaves behind.

**The one property worth a real process tree is teardown.** Everything else here is
argument construction and string handling, which a fake would cover; "Ctrl-C stops all of
it" is a claim about signals reaching processes this test does not hold a handle to, and
the way it fails is that ``vite`` or ``uvicorn`` survives its wrapper and keeps a port
bound with nothing saying why. So :class:`TestTeardownLeavesNothing` spawns a child that
spawns a grandchild of its own and asserts the grandchild is gone — the same shape as
``bin/build-images`` putting its assertion to a real container rather than to a fake.

The children here are ``sys.executable`` rather than anything of Motet's: this is a test
about process groups, and a real API would make it a test about startup validation.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.dev import (
    API_PORT_ENV,
    DEFAULT_API_PORT,
    DEFAULT_DATABASE_URL,
    DevError,
    Service,
    Supervisor,
    _redacted,
    build_services,
    check_ports,
    ensure_web_dependencies,
    env_file_path,
    parse_args,
    port_is_free,
    read_env_file,
    resolve_database_url,
)

#: A child that forks a grandchild and reports its pid, then waits forever. The
#: grandchild inherits the child's process group, which is the thing teardown has to
#: reach: `uv run` and `npm run dev` are exactly this shape.
SPAWNS_A_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print("PID", child.pid, flush=True)
time.sleep(300)
"""

#: A child that refuses SIGTERM — and says so only once the handler is actually installed.
#: Without that line the test races Python's startup and passes for the wrong reason: a
#: SIGTERM delivered a millisecond early is handled by the default disposition and the
#: child dies politely, proving nothing about the SIGKILL behind the grace period.
IGNORES_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("READY", flush=True)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    """Whether a pid is still a live process, zombies excluded.

    A child reaped by :class:`Supervisor` leaves nothing, but a *grandchild* is reparented
    to init and stays killable, so ``os.kill(pid, 0)`` is the honest question here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — not reachable for our own children
        return True
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestTeardownLeavesNothing:
    """Ctrl-C has to reach the process actually holding the port, not just the wrapper."""

    def test_a_grandchild_does_not_survive_shutdown(self) -> None:
        out = io.StringIO()
        service = Service(name="tree", argv=(sys.executable, "-u", "-c", SPAWNS_A_GRANDCHILD))
        supervisor = Supervisor([service], out=out, colour=False, grace_seconds=5.0)
        supervisor.start()
        grandchild = _read_marker_pid(out)
        assert _alive(grandchild)

        supervisor.shutdown()

        assert _wait_gone(grandchild), "the grandchild outlived shutdown — an orphan"
        assert supervisor.poll() is not None

    def test_a_child_that_ignores_sigterm_is_killed(self) -> None:
        """The grace period has a SIGKILL behind it, or a wedged child hangs the script."""
        out = io.StringIO()
        supervisor = Supervisor(
            [Service(name="stubborn", argv=(sys.executable, "-u", "-c", IGNORES_SIGTERM))],
            out=out,
            colour=False,
            grace_seconds=0.5,
        )
        supervisor.start()
        _wait_for(out, "stubborn", "READY")
        started = time.monotonic()

        supervisor.shutdown()

        assert supervisor.poll() is not None
        assert time.monotonic() - started < 20.0
        assert "killing its process group" in out.getvalue()

    def test_shutdown_is_idempotent(self) -> None:
        """`run` shuts down on its way out of both arms; a second call must be harmless."""
        out = io.StringIO()
        supervisor = Supervisor(
            [Service(name="brief", argv=(sys.executable, "-c", "pass"))],
            out=out,
            colour=False,
            grace_seconds=1.0,
        )
        supervisor.start()
        supervisor.shutdown()
        supervisor.shutdown()


class TestRun:
    """The two ways the supervised run ends."""

    def test_a_child_exiting_brings_the_rest_down(self) -> None:
        out = io.StringIO()
        supervisor = Supervisor(
            [
                Service(name="brief", argv=(sys.executable, "-c", "raise SystemExit(3)")),
                Service(name="tree", argv=(sys.executable, "-u", "-c", SPAWNS_A_GRANDCHILD)),
            ],
            out=out,
            colour=False,
            grace_seconds=5.0,
        )

        status = supervisor.run()

        assert status == 3
        assert "brief exited with status 3; stopping the rest" in out.getvalue()
        assert "stopping tree" in out.getvalue()
        assert all(proc.poll() is not None for proc in supervisor._procs.values())

    def test_a_signal_stops_everything_and_reports_it(self) -> None:
        """What Ctrl-C does, driven through the same handler a terminal would reach."""
        out = io.StringIO()
        supervisor = Supervisor(
            [Service(name="tree", argv=(sys.executable, "-u", "-c", SPAWNS_A_GRANDCHILD))],
            out=out,
            colour=False,
            grace_seconds=5.0,
        )
        _send_self_signal_soon(signal.SIGINT, delay=2.0)

        status = supervisor.run()

        assert status == 128 + int(signal.SIGINT)
        assert "got SIGINT; shutting down" in out.getvalue()
        # Read after the fact: `shutdown` joins the reader threads, so everything the
        # child printed before it died is already in `out`.
        assert _wait_gone(_read_marker_pid(out))

    def test_a_missing_executable_is_a_sentence(self) -> None:
        supervisor = Supervisor(
            [Service(name="nope", argv=("motet-does-not-exist",))],
            out=io.StringIO(),
            colour=False,
        )
        with pytest.raises(DevError, match="could not start nope"):
            supervisor.start()


class TestOnePortNumberReachesBothPlaces:
    """The sharp edge motet#83 names: the API's port and the Vite proxy's target."""

    def test_the_api_port_is_passed_to_uvicorn_and_to_vite(self) -> None:
        services = {
            service.name: service
            for service in build_services(
                root=Path("/repo"), api_port=8123, web_port=5173, poll_seconds=2
            )
        }

        assert "--port" in services["api"].argv
        assert services["api"].argv[services["api"].argv.index("--port") + 1] == "8123"
        assert services["web"].env[API_PORT_ENV] == "8123"

    def test_the_web_port_is_strict(self) -> None:
        """Vite silently picking 5174 breaks a registered OAuth redirect URI."""
        services = build_services(
            root=Path("/repo"), api_port=DEFAULT_API_PORT, web_port=5173, poll_seconds=2
        )
        web = next(service for service in services if service.name == "web")
        assert "--strictPort" in web.argv
        assert "5173" in web.argv

    def test_the_worker_polls_rather_than_draining_once(self) -> None:
        services = build_services(
            root=Path("/repo"), api_port=DEFAULT_API_PORT, web_port=5173, poll_seconds=7
        )
        worker = next(service for service in services if service.name == "worker")
        assert worker.argv[-3:] == ("all", "--poll-seconds", "7")

    def test_the_resolved_database_url_reaches_the_python_processes(self) -> None:
        """With no .env and nothing exported there is no DATABASE_URL to inherit at all.

        The migrate CLI answers that with a usage message, and a first `bin/dev` on a
        fresh clone failed on the database compose had just created for it.
        """
        services = {
            service.name: service
            for service in build_services(
                root=Path("/repo"),
                api_port=DEFAULT_API_PORT,
                web_port=5173,
                poll_seconds=2,
                database_url="postgresql://postgres:postgres@localhost:5432/motet_dev",
            )
        }

        for name in ("api", "worker"):
            assert services[name].env["DATABASE_URL"].endswith("/motet_dev")

    def test_without_drops_a_process(self) -> None:
        names = [
            service.name
            for service in build_services(
                root=Path("/repo"),
                api_port=DEFAULT_API_PORT,
                web_port=5173,
                poll_seconds=2,
                without=["web"],
            )
        ]
        assert names == ["api", "worker"]

    def test_the_port_flag_defaults_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(API_PORT_ENV, "9001")
        assert parse_args([]).api_port == 9001

    def test_a_busy_port_is_refused_before_anything_starts(self) -> None:
        import socket

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            port = held.getsockname()[1]
            assert not port_is_free(port)
            with pytest.raises(DevError, match=f"port already in use: {port}"):
                check_ports([Service(name="api", argv=("true",), ports=(port,))])


class TestDatabaseUrlResolution:
    """Resolved the way `uv run` resolves it, because the children go through `uv run`."""

    def test_the_default_is_the_compose_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("UV_ENV_FILE", raising=False)
        assert resolve_database_url() == DEFAULT_DATABASE_URL
        assert "motet_dev" in DEFAULT_DATABASE_URL

    def test_an_exported_variable_wins_over_the_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        environ = {"DATABASE_URL": "postgresql://from/shell", "UV_ENV_FILE": str(env_file)}
        assert resolve_database_url(environ) == "postgresql://from/shell"

    def test_the_env_file_wins_over_the_default(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# a comment\nDATABASE_URL='postgresql://from/file'\n")
        assert resolve_database_url({"UV_ENV_FILE": str(env_file)}) == "postgresql://from/file"

    def test_an_env_file_uv_will_not_read_is_not_read_here_either(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        environ = {"UV_ENV_FILE": str(env_file), "UV_NO_ENV_FILE": "1"}
        assert env_file_path(environ) is None
        assert resolve_database_url(environ) == DEFAULT_DATABASE_URL

    def test_no_env_file_is_named_means_none_is_read(self) -> None:
        assert env_file_path({}) is None

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert read_env_file(tmp_path / "absent") == {}

    def test_quotes_comments_and_export_are_handled(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# header\n\nexport A=1\nB=\"two words\"\nC='three'\nnot a pair\nD=\n")
        assert read_env_file(env_file) == {"A": "1", "B": "two words", "C": "three", "D": ""}


class TestWebDependencies:
    """`uv run` syncs the Python workspace itself; npm has no such behaviour."""

    def test_an_existing_node_modules_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "web" / "node_modules").mkdir(parents=True)
        # No npm is invoked, so this passes on a machine without one.
        assert ensure_web_dependencies(tmp_path) is False


class TestTheLineAHumanReads:
    """`bin/dev` prints a connection string; a password is not part of the report."""

    def test_a_password_is_redacted(self) -> None:
        assert _redacted(DEFAULT_DATABASE_URL) == (
            "postgresql://postgres:***@localhost:5432/motet_dev"
        )

    def test_a_url_without_credentials_is_unchanged(self) -> None:
        assert _redacted("postgresql://localhost:5432/motet_dev") == (
            "postgresql://localhost:5432/motet_dev"
        )

    def test_a_url_with_only_a_user_keeps_the_user(self) -> None:
        assert _redacted("postgresql://me@localhost/motet_dev") == (
            "postgresql://me@localhost/motet_dev"
        )


def _wait_for(out: io.StringIO, name: str, marker: str, timeout: float = 15.0) -> str:
    """Wait for a line a named child printed to arrive through the reader thread.

    Anchored on the service's own prefix rather than matched anywhere in the output:
    `Supervisor.start` echoes the command line it is about to run, and for these children
    that command line *is* a Python script containing the marker.
    """
    prefix = f"[{name}] {marker}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in out.getvalue().splitlines():
            if line.startswith(prefix):
                return line
        time.sleep(0.05)
    raise AssertionError(  # pragma: no cover — a failure path
        f"{name} never printed {marker}:\n{out.getvalue()}"
    )


def _read_marker_pid(out: io.StringIO, name: str = "tree") -> int:
    return int(_wait_for(out, name, "PID ").rsplit(" ", 1)[1])


def _send_self_signal_soon(signum: int, delay: float = 1.0) -> None:
    """Deliver a signal to this process from a thread, the way a terminal would.

    `Supervisor.run` blocks, so the signal has to come from somewhere else; `os.kill` on
    our own pid runs the handler `run` installed, which is the code under test.
    """
    import threading

    def _fire() -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signum)

    threading.Thread(target=_fire, daemon=True).start()


@pytest.fixture(autouse=True)
def _no_stray_children() -> object:
    """Fail loudly if a test in this module leaks a process, rather than leaking quietly."""
    before = _child_pids()
    yield None
    # `ps` is itself a child of this process while it runs, so its own pid is in the
    # snapshot it produced. It has been reaped by the time this line runs; a real leak has
    # not, which is what `_alive` distinguishes.
    leaked = {pid for pid in _child_pids() - before if _alive(pid)}
    assert not leaked, f"a test left child processes behind: {sorted(leaked)}"


def _child_pids() -> set[int]:
    """Direct children of this process, from the platform's own view of them."""
    try:
        listed = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover — no ps
        return set()
    mine = str(os.getpid())
    pids = set()
    for line in listed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == mine and parts[0].isdigit():
            pids.add(int(parts[0]))
    return pids
