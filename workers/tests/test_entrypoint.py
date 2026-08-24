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
guard is not possible either.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

#: The runpy warning this whole module exists to keep out of the worker's logs.
DOUBLE_EXECUTION = "found in sys.modules after import of package"


def _entry_module() -> str:
    """The module the worker image's ``ENTRYPOINT`` runs with ``python -m``.

    Read rather than hardcoded: a test that pinned ``motet_workers.runner`` here would
    keep passing while the image ran something else entirely.
    """
    entrypoints = re.findall(r"^ENTRYPOINT\s+(\[.*\])\s*$", DOCKERFILE.read_text(), re.M)
    assert len(entrypoints) == 1, f"expected exactly one ENTRYPOINT, found {entrypoints}"
    argv = [part.strip().strip('"') for part in entrypoints[0].strip("[]").split(",")]
    assert argv[:2] == ["python", "-m"], (
        f"the worker ENTRYPOINT is no longer a `python -m` invocation: {argv}. "
        "That is fine, but this test can no longer prove it starts cleanly — "
        "point it at whatever the new entry point is."
    )
    assert len(argv) == 3, f"unexpected ENTRYPOINT shape: {argv}"
    return argv[2]


def test_the_entry_point_module_is_not_imported_when_the_package_is() -> None:
    """The direct statement of the rule, and the cheapest thing to check.

    A fresh interpreter, because ``motet_workers`` is already imported in this one — this
    has to observe the *first* import of the package, which is the only one that matters.
    """
    module = _entry_module()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, motet_workers; sys.exit({module!r} in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"importing `motet_workers` pulled {module} into sys.modules. `python -m {module}`"
        " will now execute it a second time as __main__, with a second copy of every"
        " module-level object. Move whatever `__init__` re-exports out of the entry point"
        " module — that is what `motet_workers.drain` is for. See motet#21."
    )


@pytest.mark.parametrize("argument", ["--help", "integrate"])
def test_the_entry_point_starts_without_a_runtime_warning(argument: str) -> None:
    """The end-to-end version: run the entry point exactly as the image does.

    ``-W error::RuntimeWarning`` is what gives this teeth. runpy emits the double-execution
    warning through ``warnings.warn`` before the module runs at all, so turning it into an
    error makes the process exit non-zero *and* print the message — a plain run prints it
    to stderr and carries on to exit 0, which is precisely why it survived to production.

    Both arguments are exercised because they stop at different depths: ``--help`` exits
    inside argparse, while a queue name goes on to configure telemetry and validate the LLM
    config first. ``DATABASE_URL`` is unset for the second so it stops on that rather than
    draining a real queue out from under the rest of the suite; the assertion is only that
    it never *warns* on the way.
    """
    module = _entry_module()
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["MOTET_INFERENCE_MODE"] = "fake"  # invariant 7: a test never reaches a vendor.
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", module, argument],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert DOUBLE_EXECUTION not in result.stderr, (
        f"`python -m {module} {argument}` double-executes its own entry point:\n{result.stderr}"
    )
    if argument == "--help":
        assert result.returncode == 0, result.stderr
        assert "integrate" in result.stdout
    else:
        assert "DATABASE_URL is not set" in result.stderr, result.stderr


def test_the_help_output_names_something_a_person_could_type() -> None:
    """``prog``, because argparse otherwise takes it from ``sys.argv[0]``.

    Under ``python -m`` that is the file, so the usage line read ``usage: runner.py`` —
    a name that appears nowhere anyone could run.
    """
    module = _entry_module()
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: motet-worker")


def test_the_loop_and_the_entry_point_are_different_modules() -> None:
    """Belt to the braces above: ``drain`` is re-exported, so it must not live in ``runner``.

    Stated as an identity rather than a name check, so moving the loop to a third module
    is fine and moving it *back into the entry point* is not.
    """
    import motet_workers
    from motet_workers import drain, runner

    assert motet_workers.drain is drain
    assert runner.drain is drain
    assert drain.__module__ != runner.__name__, (
        "the drain loop is defined in the entry point module again — "
        "re-exporting it from `__init__` is what causes motet#21"
    )
