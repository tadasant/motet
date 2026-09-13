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

import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import tools.dev
from tools.dev import (
    API_PORT_ENV,
    DEFAULT_API_PORT,
    DEFAULT_DATABASE_URL,
    DevError,
    Service,
    Supervisor,
    _redacted,
    _run,
    build_services,
    check_ports,
    compose_up,
    ensure_web_dependencies,
    env_file_paths,
    main,
    migrate,
    parse_args,
    port_is_free,
    read_env_file,
    resolve_database_url,
    shadow_warning,
    shadowed_names,
)

#: A live-credential-shaped value, and a stale one. Neither may ever reach a terminal.
FILE_KEY = "sk-or-v1-fromthefile0000000000000000"
STALE_KEY = "sk-or-v1-stalefromzshrc111111111111"

#: A child that forks a grandchild and reports its pid, then waits forever. The
#: grandchild inherits the child's process group, which is the thing teardown has to
#: reach: `uv run` and `npm run dev` are exactly this shape.
SPAWNS_A_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print("PID", child.pid, flush=True)
time.sleep(300)
"""

#: A wrapper that spawns a grandchild and then EXITS, leaving the grandchild holding
#: whatever the group holds. This is `uv run` propagating and returning while uvicorn's
#: reloader child is still winding down, and it is the case a teardown that derives the
#: group from `os.getpgid(pid)` cannot signal at all: the leader has been reaped by then.
EXITS_BEFORE_ITS_GRANDCHILD = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print("PID", child.pid, flush=True)
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

    def test_a_grandchild_of_an_already_exited_wrapper_is_killed_too(self) -> None:
        """The orphan case a group derived at teardown silently misses.

        Once the wrapper has been reaped there is no pid-table entry to ask for a group
        id, so `os.getpgid` raises and the SIGKILL that is meant to be unconditional
        signals nothing — leaving the process that actually holds the port running.
        """
        out = io.StringIO()
        supervisor = Supervisor(
            [Service(name="tree", argv=(sys.executable, "-u", "-c", EXITS_BEFORE_ITS_GRANDCHILD))],
            out=out,
            colour=False,
            grace_seconds=2.0,
        )
        supervisor.start()
        grandchild = _read_marker_pid(out)
        assert _wait_gone(supervisor._procs["tree"].pid) or True  # the wrapper exits on its own

        supervisor.shutdown()

        assert _wait_gone(grandchild), "the wrapper was reaped and its group was never killed"

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
        with _self_signal_soon(signal.SIGINT, delay=2.0):
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
        assert resolve_database_url() == (DEFAULT_DATABASE_URL, "default")
        assert "motet_dev" in DEFAULT_DATABASE_URL

    def test_an_exported_variable_wins_over_the_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        environ = {"DATABASE_URL": "postgresql://from/shell", "UV_ENV_FILE": str(env_file)}
        assert resolve_database_url(environ) == ("postgresql://from/shell", "environment")

    def test_the_env_file_wins_over_the_default(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# a comment\nDATABASE_URL='postgresql://from/file'\n")
        assert resolve_database_url({"UV_ENV_FILE": str(env_file)}) == (
            "postgresql://from/file",
            "env file",
        )

    def test_an_env_file_uv_will_not_read_is_not_read_here_either(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        environ = {"UV_ENV_FILE": str(env_file), "UV_NO_ENV_FILE": "1"}
        assert env_file_paths(environ) == []
        assert resolve_database_url(environ) == (DEFAULT_DATABASE_URL, "default")

    def test_no_env_file_is_named_means_none_is_read(self) -> None:
        assert env_file_paths({}) == []

    def test_uv_env_file_may_name_several_files(self, tmp_path: Path) -> None:
        """`uv` takes a whitespace-separated list; one `Path()` over it reads nothing."""
        first = tmp_path / "a.env"
        first.write_text("OTHER=1\n")
        second = tmp_path / "b.env"
        second.write_text("DATABASE_URL=postgresql://from/second\n")
        environ = {"UV_ENV_FILE": f"{first} {second}"}
        assert env_file_paths(environ) == [first, second]
        assert resolve_database_url(environ) == ("postgresql://from/second", "env file")

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert read_env_file(tmp_path / "absent") == {}

    def test_quotes_comments_and_export_are_handled(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# header\n\nexport A=1\nB=\"two words\"\nC='three'\nnot a pair\nD=\n")
        assert read_env_file(env_file) == {"A": "1", "B": "two words", "C": "three", "D": ""}


class TestAnExportedNameBeatsTheEnvFile:
    """motet#85: `uv run --env-file` never overrides an exported variable, and nothing said so."""

    def test_an_exported_name_with_a_different_value_is_named(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(f"OPENROUTER_API_KEY={FILE_KEY}\nCARTESIA_API_KEY=x\n")
        environ = {"OPENROUTER_API_KEY": STALE_KEY}
        assert shadowed_names([env_file], environ) == ["OPENROUTER_API_KEY"]

    def test_the_same_value_exported_is_not_a_collision(self, tmp_path: Path) -> None:
        """`set -a; . ./.env` exports the file's own values; nothing that runs changes."""
        env_file = tmp_path / ".env"
        env_file.write_text(f"OPENROUTER_API_KEY={FILE_KEY}\n")
        assert shadowed_names([env_file], {"OPENROUTER_API_KEY": FILE_KEY}) == []

    def test_an_exported_empty_value_still_wins_and_is_named(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(f"OPENROUTER_API_KEY={FILE_KEY}\n")
        assert shadowed_names([env_file], {"OPENROUTER_API_KEY": ""}) == ["OPENROUTER_API_KEY"]

    def test_every_named_file_is_checked_and_names_are_sorted_once(self, tmp_path: Path) -> None:
        first = tmp_path / "a.env"
        first.write_text("B=1\nA=1\n")
        second = tmp_path / "b.env"
        second.write_text("A=2\nC=3\n")
        environ = {"A": "0", "B": "0", "C": "3"}
        assert shadowed_names([first, second], environ) == ["A", "B"]

    def test_the_warning_carries_names_and_never_values(self) -> None:
        text = shadow_warning(["OPENROUTER_API_KEY"], ".env")
        assert "OPENROUTER_API_KEY" in text
        assert ".env" in text
        assert "precedence" in text


class TestWebDependencies:
    """`uv run` syncs the Python workspace itself; npm has no such behaviour."""

    def test_an_existing_node_modules_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "web" / "node_modules").mkdir(parents=True)
        # No npm is invoked, so this passes on a machine without one.
        assert ensure_web_dependencies(tmp_path) is False

    def test_a_failed_install_is_a_sentence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools.dev, "_run", lambda *a, **k: 1)
        with pytest.raises(DevError, match="npm ci"):
            ensure_web_dependencies(tmp_path)


class TestTheOneShotStepsFailLoudly:
    """`main` catches DevError and nothing else, so every step has to raise one."""

    def test_a_missing_program_is_a_sentence_rather_than_a_traceback(self, tmp_path: Path) -> None:
        with pytest.raises(DevError, match="could not run"):
            _run(["motet-does-not-exist"], cwd=tmp_path)

    def test_a_missing_docker_says_what_to_do_instead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(DevError, match="--no-db"):
            compose_up(tmp_path)

    def test_a_failed_compose_up_points_at_the_old_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(tools.dev, "_run", lambda *a, **k: 1)
        with pytest.raises(DevError, match="motet-pg"):
            compose_up(tmp_path)

    def test_failed_migrations_name_the_database_without_its_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools.dev, "_run", lambda *a, **k: 1)
        with pytest.raises(DevError) as caught:
            migrate(tmp_path, "postgresql://postgres:hunter2@localhost:5432/motet_dev")
        assert "hunter2" not in str(caught.value)
        assert "motet_dev" in str(caught.value)

    def test_migrations_are_handed_the_url_through_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An argument would put the password in every `ps` on the machine."""
        seen: dict[str, object] = {}

        def _fake(argv: object, *, cwd: object, env: object = None) -> int:
            seen["argv"] = argv
            seen["env"] = env
            return 0

        monkeypatch.setattr(tools.dev, "_run", _fake)
        migrate(tmp_path, "postgresql://postgres:hunter2@localhost:5432/motet_dev")
        argv = seen["argv"]
        assert isinstance(argv, list)
        assert not any("hunter2" in part for part in argv)
        env = seen["env"]
        assert isinstance(env, dict)
        assert env["DATABASE_URL"].endswith("/motet_dev")


class TestMain:
    """The orchestration, driven with every side effect turned off."""

    def test_without_everything_does_the_steps_and_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("UV_ENV_FILE", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:hunter2@localhost/mine")

        status = main(
            [
                "--no-db",
                "--no-migrate",
                "--without",
                "api",
                "--without",
                "worker",
                "--without",
                "web",
            ]
        )

        printed = capsys.readouterr().out
        assert status == 0
        assert "nothing left to start" in printed
        assert "hunter2" not in printed
        assert "(environment)" in printed

    def test_an_unread_env_file_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ "my .env is ignored" and "I forgot the export" are the same five minutes."""
        monkeypatch.delenv("UV_ENV_FILE", raising=False)
        monkeypatch.setattr(Path, "exists", lambda self: True)

        main(
            [
                "--no-db",
                "--no-migrate",
                "--without",
                "api",
                "--without",
                "worker",
                "--without",
                "web",
            ]
        )

        assert "NOT being read" in capsys.readouterr().out

    def test_a_named_env_file_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        monkeypatch.setenv("UV_ENV_FILE", str(env_file))
        # `bin/ci` exports this to keep a real .env out of the suite (invariant 7), and it
        # is the very flag `env_file_paths` honours — so without clearing it this test
        # passes on a laptop and fails in CI.
        monkeypatch.delenv("UV_NO_ENV_FILE", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        main(
            [
                "--no-db",
                "--no-migrate",
                "--without",
                "api",
                "--without",
                "worker",
                "--without",
                "web",
            ]
        )

        printed = capsys.readouterr().out
        assert str(env_file) in printed
        assert "(env file)" in printed

    def test_a_stale_export_is_warned_about_by_name_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The motet#85 run: a `~/.zshrc` key beat the one in `.env`, and nothing said so."""
        env_file = tmp_path / ".env"
        env_file.write_text(f"OPENROUTER_API_KEY={FILE_KEY}\nDATABASE_URL=postgresql://from/file\n")
        monkeypatch.setenv("UV_ENV_FILE", str(env_file))
        monkeypatch.delenv("UV_NO_ENV_FILE", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", STALE_KEY)

        status = main(_NOTHING_TO_START)

        printed = capsys.readouterr().out
        assert status == 0
        assert "OPENROUTER_API_KEY" in printed
        assert "precedence" in printed
        assert FILE_KEY not in printed
        assert STALE_KEY not in printed
        # Warn only: DATABASE_URL still resolves from the file, as it did before.
        assert "(env file)" in printed

    def test_no_warning_when_nothing_collides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(f"OPENROUTER_API_KEY={FILE_KEY}\n")
        monkeypatch.setenv("UV_ENV_FILE", str(env_file))
        monkeypatch.delenv("UV_NO_ENV_FILE", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        main(_NOTHING_TO_START)

        assert "warning:" not in capsys.readouterr().out

    def test_an_exported_database_url_is_said_once_on_its_own_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--no-db` with an exported URL is legitimate: fold it into the label, not a warning."""
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://postgres:filepw@localhost/from_file\n")
        monkeypatch.setenv("UV_ENV_FILE", str(env_file))
        monkeypatch.delenv("UV_NO_ENV_FILE", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:shellpw@localhost/mine")

        main(_NOTHING_TO_START)

        printed = capsys.readouterr().out
        assert "warning:" not in printed
        assert "(environment, over the env file's DATABASE_URL)" in printed
        assert "filepw" not in printed
        assert "shellpw" not in printed

    def test_an_exported_empty_database_url_is_not_hidden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The label reads "env file" here, but uv will hand the children the empty export."""
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgresql://from/file\n")
        monkeypatch.setenv("UV_ENV_FILE", str(env_file))
        monkeypatch.delenv("UV_NO_ENV_FILE", raising=False)
        monkeypatch.setenv("DATABASE_URL", "")

        main(_NOTHING_TO_START)

        printed = capsys.readouterr().out
        assert "warning:" in printed
        assert "DATABASE_URL" in printed.split("warning:", 1)[1]

    def test_a_dev_error_is_one_line_and_a_nonzero_status(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(tools.dev, "compose_up", _raise_dev_error)

        status = main(
            ["--no-migrate", "--without", "api", "--without", "worker", "--without", "web"]
        )

        assert status == 1
        assert "error: nope" in capsys.readouterr().out


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


#: `main`'s arguments with every side effect turned off.
_NOTHING_TO_START = [
    "--no-db",
    "--no-migrate",
    "--without",
    "api",
    "--without",
    "worker",
    "--without",
    "web",
]


def _raise_dev_error(*_args: object, **_kwargs: object) -> None:
    raise DevError("nope")


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


@contextlib.contextmanager
def _self_signal_soon(signum: int, delay: float = 1.0) -> Iterator[None]:
    """Deliver a signal to this process from a thread, the way a terminal would.

    `Supervisor.run` blocks, so the signal has to come from somewhere else; `os.kill` on
    our own pid runs the handler `run` installed, which is the code under test.

    **Cancelled on the way out, and that is not tidiness.** `run` restores the default
    SIGINT disposition in its own `finally`, so a signal still pending after the test
    returns early — because an assertion failed, say — lands on pytest as a
    `KeyboardInterrupt` and aborts the whole suite with a cause that names the wrong
    thing.
    """
    import threading

    cancelled = threading.Event()

    def _fire() -> None:
        if not cancelled.wait(delay):
            os.kill(os.getpid(), signum)

    thread = threading.Thread(target=_fire, daemon=True)
    thread.start()
    try:
        yield
    finally:
        cancelled.set()
        thread.join(timeout=5.0)


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
