"""Object storage for episode audio.

One :class:`ObjectStore` interface, a GCS backend for deployed environments, and a local
filesystem backend for dev and CI — the same fake-by-default shape as the inference seam,
so the whole pipeline runs with no cloud and no credentials.
"""

from .store import (
    BACKEND_ENV,
    BUCKET_ENV,
    DEFAULT_SIGNED_URL_TTL_SECONDS,
    LOCAL_DIR_ENV,
    SIGNED_URL_TTL_ENV,
    GcsObjectStore,
    LocalObjectStore,
    ObjectStore,
    StorageError,
    build_store,
    episode_audio_key,
)

__all__ = [
    "BACKEND_ENV",
    "BUCKET_ENV",
    "DEFAULT_SIGNED_URL_TTL_SECONDS",
    "LOCAL_DIR_ENV",
    "SIGNED_URL_TTL_ENV",
    "GcsObjectStore",
    "LocalObjectStore",
    "ObjectStore",
    "StorageError",
    "build_store",
    "episode_audio_key",
]
