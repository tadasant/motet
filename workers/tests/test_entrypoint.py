"""The worker entry point is executed, never imported — and this is what proves it.

``python -m motet_workers.runner`` imports the package ``motet_workers`` first, and only
then executes ``runner.py`` as ``__main__``. If anything reachable from ``__init__`` has
pulled ``runner`` into ``sys.modules`` on the way past, ``runpy`` executes the same file a
second time under a second name and warns that this "may result in unpredictable
behaviour" — two copies of one module, with two copies of every module-level object. That
is motet#21, and it shipped in the production worker image.

The one-line fix is easy to make and just as easy to undo: re-exporting anything from
``runner`` in ``__init__.py`` brings it straight back, and it comes back as a warning on
stderr that a passing test suite would never notice. Hence these tests, which are worth
more than the fix.

They run the entry point **the image's ``ENTRYPOINT`` actually names**, read out of the
``Dockerfile`` rather than hardcoded here, so moving the entry point without moving this
guard is not possible either. ``bin/build-images`` makes the same check against the built
artifact, which is the only place the image's own interpreter and install layout are on
trial; this file tests the workspace.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

#: The runpy warning this whole module exists to keep out of the worker's logs.
DOUBLE_EXECUTION = "found in sys.modules after import of package"

#: Every subprocess here is expected to finish in well under a second. The bound exists so
#: that a child which somehow blocks — on an obs flush, or on a database that a future
#: default handed it — fails this test in a minute rather than hanging the CI job until
#: GitHub's six-hour limit.
TIMEOUT_SECONDS = 60


def _entry_module() -> str:
    """The module the worker image's ``ENTRYPOINT`` runs with ``python -m``.

    Read rather than hardcoded: a test that pinned ``motet_workers.runner`` here would
    keep passing while the image ran something else entirely. Scoped to the text after
    the worker stage begins, because the root Dockerfile has two targets and an
    ``ENTRYPOINT`` added to the ``api`` one is not this test's business.
    """
    dockerfile = DOCKERFILE.read_text()
    _, _, worker_stage = dockerfile.partition("\nFROM runtime AS worker\n")
    assert worker_stage, "the Dockerfile no longer has a `FROM runtime AS worker` stage"

    entrypoints = re.findall(r"^ENTRYPOINT\s+(\[.*\])\s*$", worker_stage, re.M)
    assert len(entrypoints) == 1, f"expected exactly one worker ENTRYPOINT, found {entrypoints}"
    argv = [part.strip().strip('"') for part in entrypoints[0].strip("[]").split(",")]
    assert argv[:2] == ["python", "-m"] and len(argv) == 3, (
        f"the worker ENTRYPOINT is no longer a `python -m <module>` invocation: {argv}. "
        "That is fine, but this test can no longer prove it starts cleanly — "
        "point it at whatever the new entry point is."
    )

    module = argv[2]
    # Without this the first test below passes vacuously: a module that does not exist is
    # trivially not in `sys.modules`.
    assert importlib.util.find_spec(module) is not None, (
        f"the worker ENTRYPOINT names {module}, which is not importable"
    )
    return module


def _run_entry_point(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the entry point the way the image does, in a child that exports nothing.

    ``DATABASE_URL`` is dropped so a queue name stops on the missing-database check rather
    than draining a real queue out from under the rest of the suite, and the telemetry
    variables are dropped because CI runs on a shared pool that has credentials for the
    real obs stack (invariant 11). A startup test that exported to it would put test spans
    under ``motet-worker`` — the very label an operator filters on — and the
    ``parser.error`` path exits before the ``finally`` that flushes, so the only flush
    would be an ``atexit`` hook blocking on a network round trip.

    ``-W error::RuntimeWarning`` is what gives all of this teeth: runpy emits the
    double-execution warning through ``warnings.warn`` before the module runs at all, so
    turning it into an error makes the process exit non-zero *and* print the message. A
    plain run prints it to stderr and carries on to exit 0, which is exactly how this
    reached production.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DATABASE_URL" and not key.startswith(("OTEL_", "SENTRY_DSN", "GLITCHTIP_"))
    }
    env["MOTET_INFERENCE_MODE"] = "fake"  # invariant 7: a test never reaches a vendor.
    return subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", _entry_module(), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=TIMEOUT_SECONDS,
    )


def test_the_entry_point_module_is_not_imported_when_the_package_is() -> None:
    """The direct statement of the rule, and the cheapest thing to check.

    A fresh interpreter, because ``motet_workers`` is already imported in this one — this
    has to observe the *first* import of the package, which is the only one that matters.
    """
    module = _entry_module()
    result = subprocess.run(
        [sys.executable, "-c", f"import sys, motet_workers; sys.exit({module!r} in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"importing `motet_workers` pulled {module} into sys.modules. `python -m {module}`"
        " will now execute it a second time as __main__, with a second copy of every"
        " module-level object. Move whatever `__init__` re-exports out of the entry point"
        " module — that is what `motet_workers.loop` is for. See motet#21."
    )


def test_the_entry_point_prints_help_without_a_runtime_warning() -> None:
    """The shallow run: argparse answers and exits before anything else happens."""
    result = _run_entry_point("--help")
    assert DOUBLE_EXECUTION not in result.stderr, (
        f"the entry point double-executes itself:\n{result.stderr}"
    )
    assert result.returncode == 0, result.stderr
    assert "integrate" in result.stdout


def test_the_entry_point_reaches_its_startup_checks_without_a_runtime_warning() -> None:
    """The deep run: a queue name gets past argparse into obs and LLM configuration.

    Exit 2 is argparse's, from the ``DATABASE_URL`` check — which is as far as this is
    meant to get. The message is asserted too, but second: rewording it should not fail a
    test about warnings.
    """
    result = _run_entry_point("integrate")
    assert DOUBLE_EXECUTION not in result.stderr, (
        f"the entry point double-executes itself:\n{result.stderr}"
    )
    assert result.returncode == 2, result.stderr
    assert "DATABASE_URL" in result.stderr, result.stderr


def test_the_help_output_names_something_a_person_could_type() -> None:
    """``prog``, because argparse otherwise takes it from ``sys.argv[0]``.

    Under ``python -m`` that is the file, so the usage line read ``usage: runner.py`` —
    a name that appears nowhere anyone could run.
    """
    result = _run_entry_point("--help")
    assert result.stdout.startswith("usage: motet-worker"), result.stdout


def test_the_loop_and_the_entry_point_are_different_modules() -> None:
    """Belt to the braces above: ``drain`` is re-exported, so it must not live in ``runner``.

    Stated as an identity rather than a name check, so moving the loop to a third module
    is fine and moving it *back into the entry point* is not.
    """
    from motet_workers import drain, runner

    assert inspect.isfunction(drain), (
        "`motet_workers.__init__` no longer re-exports the drain loop; these guards assume"
        " it does, because that re-export is what made motet#21 possible"
    )
    assert runner.drain is drain
    assert drain.__module__ != runner.__name__, (
        "the drain loop is defined in the entry point module again — "
        "re-exporting it from `__init__` is what causes motet#21"
    )
