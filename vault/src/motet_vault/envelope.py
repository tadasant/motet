"""Envelope encryption for third-party source credentials.

**Invariant 8.** A Gmail refresh token is the first secret Motet holds that belongs to
somebody else, and it is never plaintext at rest. Every record carries its own data
encryption key (DEK); the DEK is wrapped by a key encryption key (KEK) that lives in Cloud
KMS and never leaves it. The database therefore holds two ciphertexts and no key.

    plaintext  --AES-256-GCM(DEK, AAD)-->  ciphertext        (stored)
    DEK        --KMS.encrypt(KEK, AAD)-->  wrapped DEK       (stored)
    DEK                                                      (never stored)

**The AAD is the whole point of the design, not decoration.** It binds a record's
ciphertext to ``user_id:source_id:provider``. Copying row A's ciphertext over row B and
asking a worker to decrypt it produces an authentication failure rather than B's account
holding A's mailbox — a swap that plain encryption would happily perform.

**Wrapping and unwrapping are two Protocols, deliberately.** Invariant 8 says only workers
may decrypt, and Cloud KMS distinguishes ``useToEncrypt`` from ``useToDecrypt``, so the
IAM grant can be — and is — asymmetric. Splitting the interface makes that boundary a
thing the type checker sees: the API takes a :class:`DekWrapper` and cannot ask for
plaintext back, because the method does not exist on what it holds. **The IAM grant is the
actual control**; this split is what stops a well-meaning refactor from quietly needing
the grant widened.

**The KEK is not provisioned yet**, so the shipped default is :class:`LocalKeyManager` —
a fake in exactly the sense the inference fakes are fakes. It implements the contract
honestly with a local KEK instead of a hosted one, so the whole path is exercised in CI,
and it *refuses to be selected* when the process is in real mode. Turning the real one on
is one environment variable.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("motet.vault")

BACKEND_ENV: Final = "MOTET_VAULT_BACKEND"
KMS_KEY_ENV: Final = "MOTET_VAULT_KMS_KEY"
LOCAL_KEK_ENV: Final = "MOTET_VAULT_LOCAL_KEK"

#: AES-256. Not configurable: an algorithm agility knob is a downgrade attack waiting for
#: a misconfiguration, and there is exactly one algorithm this system needs.
DEK_BYTES: Final = 32
NONCE_BYTES: Final = 12

#: What ``backend`` records on a row, so a future key rotation can tell which records were
#: written under which scheme without guessing from the ciphertext.
LOCAL_BACKEND: Final = "local"
KMS_BACKEND: Final = "kms"


class VaultError(RuntimeError):
    """The vault could not do what was asked."""


class VaultConfigError(VaultError):
    """The vault is not configured well enough to be used at all."""


class DecryptionError(VaultError):
    """A ciphertext did not authenticate.

    Either the record was tampered with, or it was decrypted under the wrong AAD — which
    is what a ciphertext moved between rows looks like. Both are the same answer: this is
    not the secret you asked for, so you do not get one.
    """


def aad(*, user_id: str, source_id: str, provider: str) -> bytes:
    """The additional authenticated data every record is bound to.

    ``user_id:source_id:provider``, exactly as invariant 8 states it. Built here rather
    than at each call site because a caller that assembled it slightly differently would
    write records nothing could ever decrypt, and the failure would arrive at the next
    poll rather than at the write.
    """
    for name, value in (("user_id", user_id), ("source_id", source_id), ("provider", provider)):
        if not value or ":" in value:
            raise VaultError(f"{name}={value!r} is empty or contains ':', so the AAD is ambiguous")
    return f"{user_id}:{source_id}:{provider}".encode()


@dataclass(frozen=True)
class SealedSecret:
    """What actually goes in the database. No plaintext, no key.

    ``backend`` and ``key_name`` are provenance rather than configuration: they say which
    KEK sealed this record, so a re-key can find the records that still need doing. They
    are *not* consulted when unsealing — the process's configured key manager is — because
    a row that could choose its own decryptor is a row that could choose a weaker one.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    backend: str
    key_name: str


@runtime_checkable
class DekWrapper(Protocol):
    """Can turn a fresh DEK into a wrapped one. **Encrypt only.**

    This is what the API holds. The OAuth callback lands on an HTTP route, so the API is
    where a token first arrives and therefore where it must be sealed — but sealing is all
    it may ever do.
    """

    @property
    def backend(self) -> str: ...

    @property
    def key_name(self) -> str: ...

    def wrap(self, dek: bytes, associated_data: bytes) -> bytes: ...


@runtime_checkable
class KeyManager(DekWrapper, Protocol):
    """Wrap *and* unwrap. Workers only — this is the half invariant 8 fences off."""

    def unwrap(self, wrapped: bytes, associated_data: bytes) -> bytes: ...


def seal(wrapper: DekWrapper, plaintext: bytes, associated_data: bytes) -> SealedSecret:
    """Encrypt ``plaintext`` under a fresh per-record DEK.

    A new DEK per record, never a shared one: a single key across every record means one
    compromise is total, and it makes re-keying a single credential impossible without
    re-keying all of them.
    """
    if not plaintext:
        raise VaultError("refusing to seal an empty secret")
    dek = os.urandom(DEK_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, associated_data)
    return SealedSecret(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_dek=wrapper.wrap(dek, associated_data),
        backend=wrapper.backend,
        key_name=wrapper.key_name,
    )


def open_sealed(manager: KeyManager, sealed: SealedSecret, associated_data: bytes) -> bytes:
    """Recover the plaintext. Raises rather than returning anything on failure."""
    dek = manager.unwrap(sealed.wrapped_dek, associated_data)
    try:
        return AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, associated_data)
    except InvalidTag as exc:
        raise DecryptionError(
            "this credential did not authenticate under its own AAD; it was tampered with "
            "or moved between records"
        ) from exc


@dataclass(frozen=True)
class LocalKeyManager:
    """A KEK held in this process. Dev and CI only, and it says so out loud.

    Honest about the contract — wrap really does encrypt, unwrap really does authenticate
    the AAD — so every test exercises the same failure modes the KMS backend has. What it
    does *not* provide is the property that makes invariant 8 worth anything: a key the
    process cannot read. That is why :func:`build_key_manager` refuses to hand one out in
    real mode.
    """

    kek: bytes = field(repr=False)

    @property
    def backend(self) -> str:
        return LOCAL_BACKEND

    @property
    def key_name(self) -> str:
        """A digest of the key, not the key.

        Enough to tell two local KEKs apart when debugging, and useless to anybody who
        reads it out of a row.
        """
        return f"local:{hashlib.sha256(self.kek).hexdigest()[:16]}"

    def wrap(self, dek: bytes, associated_data: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(self.kek).encrypt(nonce, dek, associated_data)

    def unwrap(self, wrapped: bytes, associated_data: bytes) -> bytes:
        if len(wrapped) <= NONCE_BYTES:
            raise DecryptionError("wrapped DEK is too short to contain a nonce")
        try:
            return AESGCM(self.kek).decrypt(
                wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], associated_data
            )
        except InvalidTag as exc:
            raise DecryptionError("wrapped DEK did not authenticate under its AAD") from exc

    def __repr__(self) -> str:
        return f"LocalKeyManager(key_name={self.key_name!r}, kek=<redacted>)"


class CloudKmsKeyManager:
    """The real KEK: a Cloud KMS symmetric key that never leaves Google's boundary.

    **Dormant until the keyring exists.** Nothing in this repo names the key — it arrives
    as ``MOTET_VAULT_KMS_KEY``, a full resource path set by the service definition in the
    private infrastructure repo, because a key path is infrastructure topology and this
    repo is public.

    KMS's own ``additional_authenticated_data`` is used for the wrap, so the AAD binding
    is enforced by the service rather than by us. A wrapped DEK lifted from another row
    fails inside KMS and never becomes a key here.
    """

    def __init__(self, key_name: str) -> None:
        if not key_name:
            raise VaultConfigError(
                f"{KMS_KEY_ENV} must be set to the KMS key resource path to use the kms "
                "backend. It is injected by the service definition (private "
                "infrastructure repo); this repo never names a key."
            )
        self._key_name = key_name
        self._client: Any | None = None

    @property
    def backend(self) -> str:
        return KMS_BACKEND

    @property
    def key_name(self) -> str:
        return self._key_name

    def _kms(self) -> Any:
        if self._client is None:
            # Imported here so a local-backend process — every test, every laptop — never
            # pulls in the cloud SDK. Same shape as the GCS backend in `motet_storage`.
            from google.cloud import kms  # noqa: PLC0415

            self._client = kms.KeyManagementServiceClient()
        return self._client

    def wrap(self, dek: bytes, associated_data: bytes) -> bytes:
        response = self._kms().encrypt(
            request={
                "name": self._key_name,
                "plaintext": dek,
                "additional_authenticated_data": associated_data,
            }
        )
        wrapped = response.ciphertext
        assert isinstance(wrapped, bytes)
        return wrapped

    def unwrap(self, wrapped: bytes, associated_data: bytes) -> bytes:
        # Reaching this in the API is the failure invariant 8 exists to prevent, and IAM
        # is what stops it: the runtime service account there holds `useToEncrypt` and
        # not `useToDecrypt`, so this call returns PermissionDenied rather than a key.
        response = self._kms().decrypt(
            request={
                "name": self._key_name,
                "ciphertext": wrapped,
                "additional_authenticated_data": associated_data,
            }
        )
        dek = response.plaintext
        assert isinstance(dek, bytes)
        return dek


def build_key_manager(env: Mapping[str, str] | None = None) -> KeyManager:
    """Resolve the process's key manager. Local unless told otherwise, and never in prod.

    Defaults to ``local`` for the same reason ``MOTET_INFERENCE_MODE`` defaults to
    ``fake``: a forgotten variable must fail toward the offline, free, no-credential side.
    Unlike those, a forgotten variable here *also* has to fail toward the safe side, and
    "safe" points the other way — a deployed environment quietly encrypting real Gmail
    tokens under a key in its own memory would satisfy every test and none of invariant 8.
    So real mode plus the local backend is refused outright.
    """
    from motet_inference.mode import current_mode  # noqa: PLC0415

    environ = dict(os.environ) if env is None else dict(env)
    backend = environ.get(BACKEND_ENV, LOCAL_BACKEND).strip().lower()
    real = current_mode(environ) == "real"

    if backend == KMS_BACKEND:
        return CloudKmsKeyManager(environ.get(KMS_KEY_ENV, "").strip())
    if backend == LOCAL_BACKEND:
        if real:
            raise VaultConfigError(
                f"{BACKEND_ENV}=local is refused when MOTET_INFERENCE_MODE=real: it would "
                "hold the key encryption key in this process's own memory, which is "
                "exactly what invariant 8 forbids. Set "
                f"{BACKEND_ENV}=kms and {KMS_KEY_ENV} to the provisioned key."
            )
        return LocalKeyManager(kek=_local_kek(environ))
    raise VaultConfigError(f"{BACKEND_ENV}={backend!r} is not one of: local, kms")


def build_dek_wrapper(env: Mapping[str, str] | None = None) -> DekWrapper:
    """The encrypt-only half, for the API.

    Returns the same object narrowed to the wrapping half of the contract. The narrowing
    is what the API's callers see, so a route cannot reach for ``unwrap`` without the
    change being visible in a type signature — and IAM refuses it even then.
    """
    return build_key_manager(env)


def _local_kek(environ: Mapping[str, str]) -> bytes:
    """The local KEK: from the environment, or a fixed development one.

    A fixed fallback rather than a random one per process, because a random KEK would make
    every restart lose every stored credential — which reads as data corruption rather
    than as "you are on the fake backend". It is a constant in a public repo and therefore
    not a secret; nothing but dev and CI can ever select this branch.
    """
    raw = environ.get(LOCAL_KEK_ENV, "").strip()
    if not raw:
        return hashlib.sha256(b"motet-local-development-kek").digest()
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 — base64 raises several unrelated types
        raise VaultConfigError(f"{LOCAL_KEK_ENV} is not valid base64: {exc}") from exc
    if len(kek) != DEK_BYTES:
        raise VaultConfigError(
            f"{LOCAL_KEK_ENV} decodes to {len(kek)} bytes; AES-256 needs exactly {DEK_BYTES}"
        )
    return kek
