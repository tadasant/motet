"""Write the ``.env`` a real-mode local run needs, from one service account key.

**The promise is that a laptop needs exactly one non-public thing on it** — a service
account key with read access to the local-dev secrets (motet#79). Minting that key is a
one-time, human-owned step (invariant 9); everything after it is this script.

What it does, in order:

1. Reads ``GOOGLE_APPLICATION_CREDENTIALS`` and takes the project id from **the key file's
   own** ``project_id`` field. Nothing in this repo names the project — the key does, and
   that is what keeps a public repo free of the estate's topology.
2. Lists every Secret Manager secret in that project labelled ``motet-local=true`` and
   reads the latest version of each. **Which secrets carry the label is the private repo's
   decision**, which is what keeps the roster out of this one: this script discovers a
   roster rather than restating one.
3. Appends :data:`LOCAL_OVERRIDES` — the handful of variables that have to point at
   ``localhost`` rather than at staging. Those are application knowledge and belong here.

Three guards are load-bearing rather than polish, because the file this writes holds real
vendor keys:

* **No value is ever printed.** Not on success, not in an error message, not in a
  traceback. The names travel; the values only ever go to the file.
* **An existing file is never overwritten** without ``--force``.
* **The file is written ``0600``**, by :func:`os.open` with that mode *and* an
  :func:`os.fchmod` behind it, because the mode argument alone is masked by the umask.
  Nothing is written to the descriptor in between, so the only thing the window exposes is
  an empty file.

Using the SDK rather than shelling out to ``gcloud`` is what keeps the promise at *one*
thing: ``gcloud`` would be a second install and a second login.

Run it through ``bin/local-env``; see CONTRIBUTING.md for the whole local real-mode loop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import secretmanager

from tools.dev import shadow_warning, shadowed_names

#: What the SDK raises that this script should report as a sentence. Everything Google
#: throws on a first run descends from one of these: a malformed key file and an unusable
#: identity from ``google.auth``, and a refused, missing or unreachable API from
#: ``google.api_core``. The lesson is ``motet-vault[kms]``'s, one seam along — a backend
#: that lets ``PermissionDenied`` escape as itself is a backend whose callers cannot catch
#: it. **The text of the exception is never repeated**: a KMS or Secret Manager refusal
#: quotes the full resource name, which is a project id, and this script exists partly to
#: keep that out of a terminal.
VENDOR_ERRORS = (auth_exceptions.GoogleAuthError, api_exceptions.GoogleAPIError, OSError)

#: The label a secret must carry to be pulled onto a laptop. Applied in the private
#: infrastructure repo (tadasant-internal#2804); this repo only ever reads it.
LABEL_FILTER = "labels.motet-local=true"

#: Where the file goes when ``--output`` is not given, relative to the repo root that
#: ``bin/local-env`` has already ``cd``-ed to.
DEFAULT_OUTPUT = Path(".env")

#: A secret id is ``[A-Za-z0-9_-]``; an environment variable name is narrower. A secret
#: whose id cannot be one is a mislabelled secret, and saying so by name is more use than
#: writing a line no shell will read.
ENV_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

#: Values matching this are written bare; anything else is double-quoted and escaped by
#: :func:`_render_value`. Deliberately conservative — the cost of quoting a value that did
#: not need it is nothing, and the cost of not quoting one that did is a broken ``.env``.
BARE_VALUE = re.compile(r"\A[A-Za-z0-9_@%+=:,./-]*\Z")

#: The variables :data:`LOCAL_OVERRIDES` *sets*. Kept beside the block rather than parsed
#: out of it so that the drop below is a stated list; ``test_local_env`` asserts the two
#: agree, so they cannot drift.
OVERRIDE_NAMES = (
    "DATABASE_URL",
    "MOTET_INFERENCE_MODE",
    "MOTET_VAULT_BACKEND",
    "MOTET_STORAGE_BACKEND",
    "MOTET_STORAGE_DIR",
    "MOTET_PUBLIC_BASE_URL",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "MOTET_VOICE_API_BASE_URL",
)

#: The variables the block deliberately leaves *unset*. They are reserved for the same
#: reason the ones above are: a labelled secret carrying ``MOTET_API_TOKEN`` would lock a
#: laptop's API behind staging's bearer, and one carrying ``MOTET_DRAIN_TRIGGER`` would
#: have the API try to start a Cloud Run job that is not there. "Unset" has to mean unset,
#: so it is enforced rather than described.
UNSET_NAMES = (
    "MOTET_APP_BASE_URL",
    "MOTET_API_TOKEN",
    "MOTET_DRAIN_TRIGGER",
)

#: Every name this script owns. A labelled secret carrying one of these is dropped, with a
#: line naming it, rather than allowed to point a laptop at staging's topology.
RESERVED_NAMES = frozenset(OVERRIDE_NAMES) | frozenset(UNSET_NAMES)

HEADER = """\
# Written by bin/local-env. Real values — never commit this file.
#
# The secrets below are the latest version of every Secret Manager secret labelled
# motet-local=true in the project named by GOOGLE_APPLICATION_CREDENTIALS. Which
# secrets carry that label is the private infrastructure repo's decision; re-run
# bin/local-env --force to pick up a change to the roster or a rotated value.
#
# .env.example remains the documented shape for someone working without a key.
"""

LOCAL_OVERRIDES = """\

# --- Local overrides ---------------------------------------------------------
# Everything below is application knowledge rather than a secret, and it is what
# turns staging's values into a laptop's. It is appended last and is authoritative:
# a labelled secret carrying one of these names is dropped rather than written.

# A local Postgres, never Cloud SQL.
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/motet_dev

# The point of the exercise. Real adapters, real vendor calls, real spend — against
# staging's OpenRouter and Cartesia caps, shared with staging.
MOTET_INFERENCE_MODE=real

# The `local` vault backend is refused in real mode, correctly (invariant 8). With a
# service account that holds encrypt and decrypt on the KEK, the real one works.
MOTET_VAULT_BACKEND=kms

# Audio into a directory in this checkout, not into the staging bucket.
MOTET_STORAGE_BACKEND=local
MOTET_STORAGE_DIR=.motet-storage

# Enclosure and audio URLs point at the local API, not at a deployed one.
MOTET_PUBLIC_BASE_URL=http://localhost:8000

# Keeps laptop telemetry out of the staging panels, and makes /internal/health report
# `revision: local` rather than a commit that never built this tree.
OTEL_SERVICE_NAME=motet-local
OTEL_RESOURCE_ATTRIBUTES=service.version=local

# Only matters if the voice service is run; harmless otherwise.
MOTET_VOICE_API_BASE_URL=http://localhost:8000

# Deliberately unset, each for its own reason:
#
#   MOTET_APP_BASE_URL   the Vite dev server proxies /v1, so nothing is cross-origin
#                        and there is no CORS entry to make.
#   MOTET_API_TOKEN      the API is open on a laptop; sign-in covers the browser.
#   MOTET_DRAIN_TRIGGER  there is no Cloud Run job to nudge — the worker runs in poll
#                        mode instead (`runner all --poll-seconds 2`).
"""


class LocalEnvError(Exception):
    """Something the developer can fix, reported as a sentence rather than a traceback.

    **Never carries a secret value.** Every message here is built from a name, a path, or
    a count.
    """


@dataclass(frozen=True)
class Secret:
    """One labelled secret, already resolved to its latest version.

    ``value`` is ``repr=False`` and that is the docstring above being structural rather
    than aspirational: a dataclass repr prints its fields, so ``pytest --showlocals``, a
    debugger frame, or any future log line that formats one of these would put an API key
    on a terminal. The one object that can break the no-value promise is the one that
    holds a value.
    """

    name: str
    value: str = field(repr=False)


class SecretReader(Protocol):
    """The seam to Secret Manager, narrow enough that a test's fake is honest.

    Two operations, because two is all this needs: which secrets carry the label, and what
    is in the latest version of one. A fake that had to model the SDK's request objects
    would be a worse Secret Manager rather than a better test.
    """

    def labelled(self, project_id: str) -> Sequence[str]:
        """Secret ids in ``project_id`` carrying :data:`LABEL_FILTER`, in any order."""

    def value(self, project_id: str, name: str) -> str:
        """The latest version of secret ``name``, decoded as UTF-8."""


class SecretManagerClient(Protocol):
    """The two SDK calls this uses, so a test can hand :class:`SecretManagerReader` a stub.

    The adapter itself — the filter string, the ``versions/latest`` alias, the id parsed
    off a resource name — is the part no fake can cover and the part a typo ships green,
    so it is driven over a recording stub in ``tools/tests``. Same argument as
    ``api/tests/test_drain.py`` asserting the bytes on the wire: a claim about a request
    has to be made against a request.
    """

    def list_secrets(self, *, request: Any) -> Iterable[Any]: ...

    def access_secret_version(self, *, request: Any) -> Any: ...


class SecretManagerReader:
    """:class:`SecretReader` over the real Google SDK.

    The import is at module scope, not inside a method: ``google-cloud-secret-manager`` is
    a declared dev dependency, so a missing one is a broken checkout that should say so at
    startup rather than halfway through a run holding a half-written file (the
    ``motet-vault[kms]`` lesson — a lazy import is a statement about when, never about
    whether).
    """

    def __init__(self, client: SecretManagerClient | None = None) -> None:
        if client is not None:
            self._client: SecretManagerClient = client
            return
        try:
            self._client = secretmanager.SecretManagerServiceClient()
        except VENDOR_ERRORS as exc:
            raise LocalEnvError(
                f"cannot build a Secret Manager client from the key file "
                f"({type(exc).__name__}). Check that GOOGLE_APPLICATION_CREDENTIALS "
                "points at a service account key rather than another kind of credential."
            ) from exc

    def labelled(self, project_id: str) -> Sequence[str]:
        request = secretmanager.ListSecretsRequest(
            parent=f"projects/{project_id}", filter=LABEL_FILTER
        )
        try:
            secrets = list(self._client.list_secrets(request=request))
        except VENDOR_ERRORS as exc:
            raise LocalEnvError(
                f"cannot list the secrets labelled {LABEL_FILTER} ({type(exc).__name__}). "
                "The service account needs secretmanager.secrets.list on the project, and "
                "the labels are applied in the private infrastructure repo."
            ) from exc
        return [secret.name.rsplit("/", 1)[-1] for secret in secrets]

    def value(self, project_id: str, name: str) -> str:
        request = secretmanager.AccessSecretVersionRequest(
            name=f"projects/{project_id}/secrets/{name}/versions/latest"
        )
        try:
            payload = self._client.access_secret_version(request=request).payload.data
        except VENDOR_ERRORS as exc:
            raise LocalEnvError(
                f"cannot read the latest version of secret {name} ({type(exc).__name__}). "
                "The service account needs secretmanager.versions.access on it."
            ) from exc
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalEnvError(
                f"secret {name} is not UTF-8 text; it cannot be an .env value"
            ) from exc


def project_id_from_key() -> str:
    """The project id out of the service account key ``GOOGLE_APPLICATION_CREDENTIALS`` names.

    The key file is the only thing that names the project. Reading it from there rather
    than from a constant is what lets this script live in a public repo.
    """
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw:
        raise LocalEnvError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set. Point it at the service account "
            "key for local development — see the 'Local development, real mode' section "
            "of CONTRIBUTING.md. Minting that key is a human step; this script does not."
        )
    path = Path(raw).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalEnvError(f"cannot read the key file at {path}: {exc.strerror}") from exc
    try:
        key = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalEnvError(f"the key file at {path} is not JSON: {exc.msg}") from exc
    if not isinstance(key, dict):
        raise LocalEnvError(f"the key file at {path} is not a JSON object")
    project_id = key.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise LocalEnvError(
            f"the key file at {path} has no project_id. A service account key has one; "
            "an OAuth client secret or an ADC user credential does not."
        )
    return project_id


def collect(reader: SecretReader, project_id: str) -> tuple[list[Secret], list[str]]:
    """Every labelled secret, and the names dropped for colliding with a local override.

    Returns the kept secrets sorted by name — a stable ``.env`` makes a re-run's diff
    readable — and the dropped names, which the caller reports.
    """
    names = sorted(reader.labelled(project_id))
    if not names:
        raise LocalEnvError(
            "no secret in this project carries the label motet-local=true. The labels are "
            "applied in the private infrastructure repo (tadasant-internal#2804); until "
            "that lands there is nothing here to read."
        )
    kept: list[Secret] = []
    dropped: list[str] = []
    for name in names:
        if not ENV_NAME.match(name):
            raise LocalEnvError(
                f"secret {name!r} is labelled motet-local=true but is not a usable "
                "environment variable name. Rename it, or take the label off it."
            )
        if name in RESERVED_NAMES:
            dropped.append(name)
            continue
        kept.append(Secret(name, _trim(reader.value(project_id, name))))
    if not kept:
        raise LocalEnvError(
            "every labelled secret collides with a local override, so there is nothing to write"
        )
    return kept, dropped


def _trim(value: str) -> str:
    """Drop a trailing newline, which is what ``--data-file=-`` puts on a pasted secret.

    Only the trailing newline, and only one: an API key with ``\\n`` on the end fails at a
    vendor with an authentication error that points nowhere near here, and no value this
    file carries is meant to end in one. Nothing else about the value is touched.
    """
    return value[:-1] if value.endswith("\n") else value


def _render_value(value: str) -> str:
    """A value as one ``.env`` line's right-hand side, quoted only when it has to be.

    The escapes are the four that uv's ``--env-file`` parser reads back verbatim:
    backslash, double quote, ``$`` (which would otherwise interpolate), and newline.
    """
    if BARE_VALUE.match(value):
        return value
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("\n", "\\n")
    )
    return f'"{escaped}"'


def render(secrets: Sequence[Secret]) -> str:
    """The whole file: header, the secrets, then the localhost overrides."""
    lines = [HEADER]
    lines.extend(f"{secret.name}={_render_value(secret.value)}\n" for secret in secrets)
    lines.append(LOCAL_OVERRIDES)
    return "".join(lines)


def check_target(path: Path, *, force: bool) -> None:
    """Refuse to clobber an existing file without ``--force``.

    Called *before* Secret Manager as well as by :func:`write`: a run that is going to
    refuse should refuse before it reads a dozen secrets, not after.
    """
    if path.exists() and not force:
        raise LocalEnvError(f"{path} already exists. Re-run with --force to replace it.")


def write(path: Path, text: str, *, force: bool) -> None:
    """Write ``text`` to ``path`` at mode ``0600``, refusing to clobber without ``force``.

    The mode is passed to :func:`os.open` **and** applied afterwards: the mode argument is
    masked by the process umask, so on its own it is a request rather than a guarantee.
    """
    check_target(path, force=force)
    # O_NOFOLLOW because this is the one file in the repo whose content is a pile of
    # vendor keys: a symlink sitting at the target path would otherwise have --force write
    # them straight through it. O_EXCL without --force closes the gap between the check
    # above and here.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    if not force:
        flags |= os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise LocalEnvError(f"{path} already exists. Re-run with --force to replace it.") from exc
    except OSError as exc:
        raise LocalEnvError(f"cannot write {path}: {exc.strerror}") from exc
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bin/local-env",
        description=(
            "Write a .env for real-mode local development from the Secret Manager secrets "
            "labelled motet-local=true. Never prints a value."
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing file instead of refusing"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args(list(argv))


def run(argv: Sequence[str], reader: SecretReader | None = None) -> int:
    """The whole script, with the Secret Manager seam injectable for tests."""
    args = _parse(argv)
    project_id = project_id_from_key()
    check_target(args.output, force=args.force)
    secrets, dropped = collect(reader if reader is not None else SecretManagerReader(), project_id)
    write(args.output, render(secrets), force=args.force)

    # Names, counts and a path. No value reaches a terminal, a log, or a scrollback.
    plural = "" if len(secrets) == 1 else "s"
    print(f"Wrote {args.output} ({len(secrets)} secret{plural}, mode 0600).")
    print("  " + ", ".join(secret.name for secret in secrets))
    if dropped:
        print(
            f"Dropped {len(dropped)} labelled secret(s) that a local override owns: "
            + ", ".join(dropped)
        )
    # Checked against the file as written, with the parser `bin/dev` uses, so the two tools
    # cannot disagree about which names collide (motet#85). Names only, like everything above.
    shadowed = shadowed_names([args.output])
    if shadowed:
        print("Warning: " + shadow_warning(shadowed, str(args.output)))
    print(f"Load it with `export UV_ENV_FILE={args.output}`, then run the API and the worker.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except LocalEnvError as exc:
        print(f"bin/local-env: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
