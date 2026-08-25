"""Envelope encryption for credentials that belong to somebody else.

Invariant 8: source credentials are never plaintext at rest, and only workers can
decrypt. The encrypt and decrypt halves are separate Protocols so that the IAM boundary
Cloud KMS enforces is also one the type checker enforces — see
:mod:`motet_vault.envelope`.
"""

from .envelope import (
    BACKEND_ENV,
    DEK_BYTES,
    KMS_BACKEND,
    KMS_KEY_ENV,
    KMS_SDK_MODULE,
    LOCAL_BACKEND,
    LOCAL_KEK_ENV,
    NONCE_BYTES,
    CloudKmsKeyManager,
    DecryptionError,
    DekWrapper,
    KeyManager,
    LocalKeyManager,
    SealedSecret,
    VaultConfigError,
    VaultError,
    VaultStatus,
    aad,
    build_dek_wrapper,
    build_key_manager,
    kms_sdk_installed,
    open_sealed,
    seal,
    vault_status,
)

__all__ = [
    "BACKEND_ENV",
    "DEK_BYTES",
    "KMS_BACKEND",
    "KMS_KEY_ENV",
    "KMS_SDK_MODULE",
    "LOCAL_BACKEND",
    "LOCAL_KEK_ENV",
    "NONCE_BYTES",
    "CloudKmsKeyManager",
    "DecryptionError",
    "DekWrapper",
    "KeyManager",
    "LocalKeyManager",
    "SealedSecret",
    "VaultConfigError",
    "VaultError",
    "VaultStatus",
    "aad",
    "build_dek_wrapper",
    "build_key_manager",
    "kms_sdk_installed",
    "open_sealed",
    "seal",
    "vault_status",
]
