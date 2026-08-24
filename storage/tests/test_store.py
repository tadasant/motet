"""Object storage: the local backend, and the contract both backends share.

The GCS backend is not exercised here — it needs a bucket and credentials, and invariant 7
keeps both out of CI. What *is* tested is the part a second backend has to honour: the key
layout, and the fact that ``signed_url`` returning ``None`` is a legitimate answer rather
than a failure. That optionality is what lets one API route serve both backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from motet_storage import (
    BACKEND_ENV,
    BUCKET_ENV,
    LOCAL_DIR_ENV,
    GcsObjectStore,
    LocalObjectStore,
    ObjectStore,
    StorageError,
    build_store,
    episode_audio_key,
)


class TestKeys:
    def test_are_namespaced_by_user_from_the_start(self) -> None:
        """Phase 1 has one user; the key layout does not assume that.

        A key layout is painful to change once objects exist — retrofitting a tenant prefix
        means either migrating live objects or carrying a permanent special case for the
        first user.
        """
        key = episode_audio_key("motet-owner", "ep_abc", "mp3")
        assert key == "users/motet-owner/episodes/ep_abc.mp3"

    def test_tolerate_a_dotted_extension(self) -> None:
        assert episode_audio_key("u", "e", ".mp3").endswith("e.mp3")


class TestLocalStore:
    def test_round_trips_bytes(self, tmp_path: Path) -> None:
        store = LocalObjectStore(root=tmp_path)
        store.put("users/u/episodes/e.mp3", b"\xff\xfbaudio", content_type="audio/mpeg")

        assert store.exists("users/u/episodes/e.mp3")
        assert store.get("users/u/episodes/e.mp3") == b"\xff\xfbaudio"

    def test_signed_url_is_none_and_that_is_the_contract(self, tmp_path: Path) -> None:
        """``None`` means "serve the bytes yourself", and the API route relies on it.

        Making the *store* answer the question is what keeps a third backend from having to
        touch the route.
        """
        store = LocalObjectStore(root=tmp_path)
        store.put("k", b"x", content_type="text/plain")
        assert store.signed_url("k") is None

    def test_a_reader_never_sees_a_half_written_object(self, tmp_path: Path) -> None:
        """Write-then-rename: the API serves a feed while a worker renders the next episode."""
        store = LocalObjectStore(root=tmp_path)
        store.put("k", b"first", content_type="text/plain")
        store.put("k", b"second-and-longer", content_type="text/plain")
        assert store.get("k") == b"second-and-longer"
        assert not list(tmp_path.glob("*.partial"))

    def test_missing_objects_raise_rather_than_return_empty_bytes(self, tmp_path: Path) -> None:
        with pytest.raises(StorageError):
            LocalObjectStore(root=tmp_path).get("nope")

    def test_refuses_a_key_that_would_escape_the_root(self, tmp_path: Path) -> None:
        """Keys are built by this package, never by a user — but they become paths."""
        store = LocalObjectStore(root=tmp_path)
        for key in ("../outside", "/etc/passwd", "users/../../x"):
            with pytest.raises(StorageError, match="unsafe object key"):
                store.put(key, b"x", content_type="text/plain")


class TestBuildStore:
    def test_defaults_to_local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A forgotten variable must fail toward the offline, no-credential side.

        The same reasoning as ``MOTET_INFERENCE_MODE`` defaulting to ``fake``.
        """
        monkeypatch.delenv(BACKEND_ENV, raising=False)
        monkeypatch.setenv(LOCAL_DIR_ENV, str(tmp_path))
        assert isinstance(build_store(), LocalObjectStore)

    def test_gcs_needs_a_bucket_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "gcs")
        monkeypatch.delenv(BUCKET_ENV, raising=False)
        with pytest.raises(StorageError, match=BUCKET_ENV):
            build_store()

    def test_gcs_constructs_without_touching_the_cloud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SDK is imported lazily, so constructing the store makes no calls.

        A process that never reads an object never pulls in the cloud client at all.
        """
        monkeypatch.setenv(BACKEND_ENV, "gcs")
        monkeypatch.setenv(BUCKET_ENV, "some-bucket")
        assert isinstance(build_store(), GcsObjectStore)

    def test_an_unknown_backend_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "s3")
        with pytest.raises(StorageError, match="local, gcs"):
            build_store()

    def test_a_nonsense_ttl_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKEND_ENV, "gcs")
        monkeypatch.setenv(BUCKET_ENV, "b")
        monkeypatch.setenv("MOTET_SIGNED_URL_TTL_SECONDS", "-1")
        with pytest.raises(StorageError):
            build_store()


def test_both_backends_satisfy_the_protocol(tmp_path: Path) -> None:
    """Structural, so a backend that drifts from the interface fails here rather than
    at the first feed fetch in production."""
    local: ObjectStore = LocalObjectStore(root=tmp_path)
    gcs: ObjectStore = GcsObjectStore("bucket")
    assert isinstance(local, ObjectStore)
    assert isinstance(gcs, ObjectStore)
