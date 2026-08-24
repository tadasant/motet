"""Where episode audio lives: one interface, a GCS backend, and a local one.

The same shape as the inference seam and for the same reason — the deployed system puts
audio in a bucket behind signed URLs, and CI must be able to run the whole pipeline with
no cloud, no credentials, and no network.

**Signed URLs are optional, and that is the interesting part of the contract.**
:meth:`ObjectStore.signed_url` returns ``None`` when a backend has no way to hand out a
direct link. The API's audio route treats that as "serve the bytes myself" and redirects
otherwise, so an RSS enclosure URL is stable across both backends and a podcast client
cannot tell them apart. Making the *store* answer that question, rather than the route
branching on a backend name, is what keeps a third backend from touching the route.

Bucket names, project ids, and hostnames arrive from the environment. None of them belong
in this repo — see the repo split in AGENTS.md.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

logger = logging.getLogger("motet.storage")

BACKEND_ENV: Final = "MOTET_STORAGE_BACKEND"
BUCKET_ENV: Final = "MOTET_AUDIO_BUCKET"
LOCAL_DIR_ENV: Final = "MOTET_STORAGE_DIR"
SIGNED_URL_TTL_ENV: Final = "MOTET_SIGNED_URL_TTL_SECONDS"

#: Long enough that a podcast client which queued a download can still finish it, short
#: enough that a URL scraped out of a feed is not a durable handle on the audio.
DEFAULT_SIGNED_URL_TTL_SECONDS: Final = 3600

DEFAULT_LOCAL_DIR: Final = ".motet-storage"


class StorageError(RuntimeError):
    """Something in the storage layer failed, on either backend."""


@runtime_checkable
class ObjectStore(Protocol):
    """Put bytes somewhere, get them back, and — sometimes — hand out a direct link."""

    def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def signed_url(self, key: str, *, ttl_seconds: int | None = None) -> str | None:
        """A time-limited direct URL, or ``None`` if this backend cannot mint one."""
        ...


def episode_audio_key(user_id: str, episode_id: str, extension: str) -> str:
    """Where an episode's audio goes.

    Namespaced by user from the start even though Phase 1 has one. A key layout is
    painful to change once objects exist, and retrofitting a tenant prefix later means
    either a migration of live objects or a permanent special case for the first user.
    """
    return f"users/{user_id}/episodes/{episode_id}.{extension.lstrip('.')}"


@dataclass(frozen=True)
class LocalObjectStore:
    """Files under a directory. Dev and CI only — never a deployed backend.

    ``signed_url`` returns ``None``: there is no signer and no public origin, so the API
    serves these bytes itself. That is the whole reason the return type is optional.
    """

    root: Path

    def _path(self, key: str) -> Path:
        # A key is built by this package, never by a user — but it ends up as a path, so
        # a traversal would be a file write outside the root. Cheap to refuse.
        if key.startswith("/") or ".." in Path(key).parts:
            raise StorageError(f"unsafe object key: {key!r}")
        return self.root / key

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename, so a reader never sees a half-written object. The API can be
        # serving a feed while a worker is rendering the next episode.
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.replace(path)
        logger.info("stored %d bytes at %s (%s)", len(data), key, content_type)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def signed_url(self, key: str, *, ttl_seconds: int | None = None) -> str | None:
        return None

    def clear(self) -> None:
        """Drop everything. Test helper; not part of the Protocol."""
        shutil.rmtree(self.root, ignore_errors=True)


class GcsObjectStore:
    """Google Cloud Storage with V4 signed URLs — the deployed backend.

    Signing on Cloud Run has one wrinkle worth stating: the runtime service account has
    no private key locally, so ``generate_signed_url`` cannot sign offline. It signs via
    the IAM ``signBlob`` API instead, which needs the credentials' own token and the
    service account's own email — hence the explicit ``service_account_email`` and
    ``access_token``. Getting this wrong fails at the first feed fetch with an opaque
    "you need a private key" error, so it is wired explicitly rather than left to the
    library's default guess.
    """

    def __init__(self, bucket: str, *, ttl_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS) -> None:
        if not bucket:
            raise StorageError(f"{BUCKET_ENV} must be set to use the GCS backend")
        self._bucket_name = bucket
        self._ttl_seconds = ttl_seconds
        self._client: Any | None = None

    def _bucket(self) -> Any:
        if self._client is None:
            # Imported here so that a local-backend process never pulls in the cloud SDK.
            from google.cloud import storage  # noqa: PLC0415

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self._bucket().blob(key).upload_from_string(data, content_type=content_type)
        logger.info("stored %d bytes at gs://%s/%s", len(data), self._bucket_name, key)

    def get(self, key: str) -> bytes:
        blob = self._bucket().blob(key)
        data = blob.download_as_bytes()
        assert isinstance(data, bytes)
        return data

    def exists(self, key: str) -> bool:
        exists = self._bucket().blob(key).exists()
        return bool(exists)

    def signed_url(self, key: str, *, ttl_seconds: int | None = None) -> str | None:
        import google.auth  # noqa: PLC0415
        import google.auth.transport.requests  # noqa: PLC0415

        credentials, _ = google.auth.default()
        # `service_account_email` and `token` are only populated after a refresh on the
        # compute-metadata credentials Cloud Run supplies.
        credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]  # noqa: E501
        blob = self._bucket().blob(key)
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds or self._ttl_seconds),
            method="GET",
            service_account_email=getattr(credentials, "service_account_email", None),
            access_token=getattr(credentials, "token", None),
        )
        assert isinstance(url, str)
        return url


def build_store(env: dict[str, str] | None = None) -> ObjectStore:
    """Resolve the backend from the environment. Local unless told otherwise.

    Defaults to local for the same reason ``MOTET_INFERENCE_MODE`` defaults to ``fake``:
    a forgotten variable should fail toward the offline, free, no-credential side.
    """
    environ = dict(os.environ) if env is None else env
    backend = environ.get(BACKEND_ENV, "local").strip().lower()
    if backend == "local":
        return LocalObjectStore(root=Path(environ.get(LOCAL_DIR_ENV) or DEFAULT_LOCAL_DIR))
    if backend == "gcs":
        return GcsObjectStore(
            environ.get(BUCKET_ENV, "").strip(),
            ttl_seconds=_ttl(environ),
        )
    raise StorageError(f"{BACKEND_ENV}={backend!r} is not one of: local, gcs")


def _ttl(environ: dict[str, str]) -> int:
    raw = environ.get(SIGNED_URL_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_SIGNED_URL_TTL_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        raise StorageError(f"{SIGNED_URL_TTL_ENV}={raw!r} is not an integer") from None
    if seconds <= 0:
        raise StorageError(f"{SIGNED_URL_TTL_ENV}={raw!r} must be positive")
    return seconds
