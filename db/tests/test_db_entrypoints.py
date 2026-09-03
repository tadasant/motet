"""Every ``python -m motet_db.*`` entry point is executed, never imported.

``python -m motet_db.migrate`` imports the package ``motet_db`` first, and only then
executes ``migrate.py`` as ``__main__``. If anything reachable from ``__init__`` has pulled
that module into ``sys.modules`` on the way past, ``runpy`` executes the same file a second
time under a second name and warns that this "may result in unpredictable behaviour" — two
copies of one module, with two copies of every module-level object. That is motet#27 for
``migrate`` and motet#21 for ``motet_workers.runner`` before it. Both shipped: they are the
same image, since ``motet-workers`` depends on ``motet-db``.

The one-line fix is easy to make and just as easy to undo: re-exporting anything defined in
an entry point brings it straight back, and it comes back as a warning on **stderr** that a
passing test suite and a stdout-only smoke check would both sail past. Hence this module,
which is worth more than the fix.

**The entry points are discovered rather than listed**, by looking for the ``if __name__ ==
"__main__"`` guard, so a third one added to this package is covered the day it is written
rather than the day somebody remembers this file. ``workers/tests/test_entrypoint.py`` is
the same guard on the other package's entry point and explains the ``-W
error::RuntimeWarning`` idiom in full; ``bin/build-images`` makes the check against the
built artifact, which is the only place an image's own interpreter and install layout are
on trial.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import motet_db
import pytest

PACKAGE_DIR = Path(motet_db.__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The runpy warning this whole module exists to keep out of a job's logs.
DOUBLE_EXECUTION = "found in sys.modules after import of package"

#: Every subprocess here answers in well under a second. The bound exists so that a child
#: which somehow blocks — on an obs flush, or on a database a future default handed it —
#: fails this test in a minute rather than hanging the CI job until GitHub's six-hour limit.
TIMEOUT_SECONDS = 60


def _has_main_guard(source: str) -> bool:
    """Whether a module carries a top-level ``if __name__ == "__main__":``.

    Parsed rather than grepped, so that a docstring describing the idiom — every entry
    point in this package has one — is not itself mistaken for an entry point. The
    operator is checked as well as the operands, so ``__name__ != "__main__"`` does not
    answer yes; and a non-matching comparison keeps looking rather than deciding, so an
    unrelated ``if __name__ == ...`` earlier in a file cannot mask the real guard below it.
    """
    for node in ast.parse(source).body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            continue
        if all(isinstance(op, ast.Eq) for op in node.test.ops) and any(
            isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators
        ):
            return True
    return False


def _entry_point_modules() -> list[str]:
    """Every module in the package that ``python -m`` could be pointed at.

    ``rglob`` rather than ``glob``: an entry point added inside a future subpackage would
    be missed by a flat scan, and missed silently — the superset assertion below only
    notices an entry point that *disappears*, never one that was never seen.
    """
    modules = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path.name == "__init__.py" or not _has_main_guard(path.read_text()):
            continue
        dotted = ".".join(path.relative_to(PACKAGE_DIR).with_suffix("").parts)
        modules.append(f"motet_db.{dotted}")
    return modules


ENTRY_POINTS = _entry_point_modules()

#: The name each entry point announces itself by, for the ones that exist today.
#:
#: A table *as well as* the rule below, not instead of it. The rule ("not a filename") is
#: what a newly discovered entry point is held to, since nothing here can know what it
#: should be called. The table is what stops an existing command being renamed silently:
#: `usage:` is the first line an operator reads, and `workers/tests/test_entrypoint.py`
#: pins `motet-worker` the same way. A module absent from this table is checked against the
#: rule alone.
EXPECTED_PROG = {
    "motet_db.migrate": "motet-migrate",
    "motet_db.mint_session": "motet-mint-session",
}


def _run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run an entry point in a child that exports nothing it could act on.

    ``DATABASE_URL`` is dropped so that a module which somehow got past argparse would stop
    on its own missing-database check rather than touch a database the rest of the suite is
    using, and the telemetry variables are dropped because CI runs on a shared pool holding
    credentials for the real obs stack (invariant 11) — a startup test that exported to it
    would put test spans under a label an operator filters on.

    ``-W error::RuntimeWarning`` is what gives this teeth: runpy emits the double-execution
    warning through ``warnings.warn`` before the module runs at all, so turning it into an
    error makes the process exit non-zero *and* print the message. A plain run prints it to
    stderr and carries on to exit 0, which is exactly how motet#27 stayed green in ``bin/ci``
    for as long as it did.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DATABASE_URL" and not key.startswith(("OTEL_", "SENTRY_DSN", "GLITCHTIP_"))
    }
    env["MOTET_INFERENCE_MODE"] = "fake"  # invariant 7: a test never reaches a vendor.
    return subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", module, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=TIMEOUT_SECONDS,
    )


def test_the_entry_points_this_file_guards_are_the_ones_that_exist() -> None:
    """Non-vacuity, and the only place a name is written down.

    Discovery that quietly found nothing would make every test below pass without running
    anything. Stated as a superset so that adding an entry point does not fail here — the
    parametrized guards pick it up on their own — while *losing* one does.
    """
    assert {"motet_db.migrate", "motet_db.mint_session"} <= set(ENTRY_POINTS)


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_importing_the_package_does_not_import_the_entry_point(module: str) -> None:
    """The direct statement of the rule, and the cheapest thing to check.

    A fresh interpreter, because ``motet_db`` is already imported in this one — this has to
    observe the *first* import of the package, which is the only one that matters.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import sys, motet_db; sys.exit({module!r} in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"importing `motet_db` pulled {module} into sys.modules, so `python -m {module}`"
        " now executes it a second time as __main__, with a second copy of every"
        " module-level object. Move whatever `__init__` reaches out of the entry point"
        " module — that is what `motet_db.migrations` is for. See motet#27."
    )


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_the_entry_point_prints_help_without_a_runtime_warning(module: str) -> None:
    """The run an operator makes first, from a real process, with stderr captured."""
    result = _run(module, "--help")
    assert DOUBLE_EXECUTION not in result.stderr, (
        f"{module} double-executes itself:\n{result.stderr}"
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_the_help_output_names_something_a_person_could_type(module: str) -> None:
    """``prog``, because argparse otherwise takes it from ``sys.argv[0]``.

    Under ``python -m`` that is the file, so the usage line read ``usage: migrate.py`` — a
    filename rather than a command. The claim held against every entry point is that
    negative one; :data:`EXPECTED_PROG` pins the names of the two that exist.
    """
    usage = _run(module, "--help").stdout
    assert usage.startswith("usage: "), usage
    prog = usage[len("usage: ") :].split()[0]
    assert not prog.endswith(".py") and "/" not in prog, (
        f"{module} --help announces itself as {prog!r}, which is a filename rather than"
        " something anyone could run. Pass `prog=` to ArgumentParser."
    )
    if module in EXPECTED_PROG:
        assert prog == EXPECTED_PROG[module], usage


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_the_package_re_exports_nothing_defined_in_an_entry_point(module: str) -> None:
    """Belt to the braces above, and the failure stated where it is caused.

    The check above says the entry point is not in ``sys.modules``; this one says *why* it
    would be. A name in ``__all__`` that was defined in an entry point module is the import
    that puts it there — which is precisely the line motet#21 and motet#27 both were.
    """
    offenders = [
        name
        for name in motet_db.__all__
        if getattr(getattr(motet_db, name), "__module__", None) == module
    ]
    assert not offenders, (
        f"`motet_db` re-exports {offenders}, defined in the entry point {module}."
        " Move them to an importable sibling module. See motet#27."
    )
