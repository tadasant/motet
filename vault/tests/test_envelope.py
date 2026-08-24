"""Invariant 8, as assertions.

The properties that matter are not "encryption happened" — they are the ones that make
envelope encryption worth the complexity over a single key:

* a ciphertext moved between rows does not decrypt (the AAD binding),
* every record has its own DEK, so one compromise is not total,
* nothing plaintext survives in what gets stored,
* the local backend refuses to be used in real mode, because a KEK in process memory
  satisfies every test and none of the invariant,
* the encrypt-only half genuinely cannot decrypt.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from motet_vault import (
    BACKEND_ENV,
    KMS_KEY_ENV,
    LOCAL_KEK_ENV,
    NONCE_BYTES,
    CloudKmsKeyManager,
    DecryptionError,
    DekWrapper,
    KeyManager,
    LocalKeyManager,
    VaultConfigError,
    VaultError,
    aad,
    build_dek_wrapper,
    build_key_manager,
    open_sealed,
    seal,
)

SECRET = "1//0gRefreshTokenLookingThing-abcdef123456"


def manager() -> LocalKeyManager:
    return LocalKeyManager(kek=hashlib.sha256(b"test-kek").digest())


def test_a_sealed_secret_round_trips() -> None:
    key = manager()
    binding = aad(user_id="u1", source_id="src1", provider="gmail")
    sealed = seal(key, SECRET.encode(), binding)
    assert open_sealed(key, sealed, binding).decode() == SECRET


def test_the_ciphertext_contains_no_plaintext() -> None:
    """The obvious property, asserted because it is the one a refactor breaks silently."""
    key = manager()
    binding = aad(user_id="u1", source_id="src1", provider="gmail")
    sealed = seal(key, SECRET.encode(), binding)
    blob = sealed.ciphertext + sealed.nonce + sealed.wrapped_dek
    assert SECRET.encode() not in blob
    # Not even a recognizable prefix. A refactor that accidentally stored a truncated
    # token, or that encrypted only part of it, would pass the check above.
    assert SECRET.encode()[:12] not in blob


@pytest.mark.parametrize(
    ("user_id", "source_id", "provider"),
    [
        ("u2", "src1", "gmail"),  # another user's row
        ("u1", "src2", "gmail"),  # another source on the same user
        ("u1", "src1", "outlook"),  # the same row, relabelled
    ],
)
def test_a_ciphertext_moved_to_another_row_does_not_decrypt(
    user_id: str, source_id: str, provider: str
) -> None:
    """The AAD binding, which is the entire reason this is not just AES-GCM.

    Copying row A's ciphertext into row B must not hand B's owner A's mailbox. Every axis
    of the binding is checked, because a binding that omitted one — say, provider — would
    look correct and be exploitable along exactly that axis.
    """
    key = manager()
    sealed = seal(key, SECRET.encode(), aad(user_id="u1", source_id="src1", provider="gmail"))
    with pytest.raises(DecryptionError):
        open_sealed(key, sealed, aad(user_id=user_id, source_id=source_id, provider=provider))


def test_every_record_gets_its_own_dek() -> None:
    """Two seals of the same plaintext share nothing.

    A shared DEK would make one compromise total and make re-keying a single credential
    impossible. Comparing the wrapped DEKs is the observable form of that.
    """
    key = manager()
    binding = aad(user_id="u1", source_id="src1", provider="gmail")
    first, second = (seal(key, SECRET.encode(), binding) for _ in range(2))
    assert first.wrapped_dek != second.wrapped_dek
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce


def test_a_tampered_ciphertext_is_refused() -> None:
    key = manager()
    binding = aad(user_id="u1", source_id="src1", provider="gmail")
    sealed = seal(key, SECRET.encode(), binding)
    flipped = bytearray(sealed.ciphertext)
    flipped[0] ^= 0x01
    with pytest.raises(DecryptionError):
        open_sealed(
            key,
            type(sealed)(
                ciphertext=bytes(flipped),
                nonce=sealed.nonce,
                wrapped_dek=sealed.wrapped_dek,
                backend=sealed.backend,
                key_name=sealed.key_name,
            ),
            binding,
        )


def test_a_truncated_wrapped_dek_is_refused() -> None:
    key = manager()
    with pytest.raises(DecryptionError):
        key.unwrap(b"\x00" * (NONCE_BYTES - 1), b"aad")


def test_the_aad_refuses_ambiguous_components() -> None:
    """A colon inside a component would make the binding ambiguous.

    ``a:b`` + ``c`` and ``a`` + ``b:c`` would produce the same AAD, which is exactly the
    confusion the binding exists to prevent — so it is refused at construction.
    """
    with pytest.raises(VaultError):
        aad(user_id="u:1", source_id="src1", provider="gmail")
    with pytest.raises(VaultError):
        aad(user_id="", source_id="src1", provider="gmail")


def test_refusing_to_seal_an_empty_secret() -> None:
    with pytest.raises(VaultError):
        seal(manager(), b"", b"aad")


def test_the_key_name_is_a_digest_not_the_key() -> None:
    """A row records which KEK sealed it; it must not record the KEK."""
    key = manager()
    assert key.key_name.startswith("local:")
    assert base64.b64encode(key.kek).decode() not in key.key_name
    assert key.kek.hex() not in key.key_name


def test_the_key_manager_redacts_itself() -> None:
    """A traceback or a debug log must not print the KEK."""
    text = repr(manager())
    assert "kek=<redacted>" in text
    assert manager().kek.hex() not in text


# --- backend selection ---------------------------------------------------------------


def test_local_is_the_default() -> None:
    """A forgotten variable fails toward the offline, no-credential side."""
    assert build_key_manager({}).backend == "local"


def test_the_local_backend_is_refused_in_real_mode() -> None:
    """The one that actually protects the invariant.

    A deployed environment holding the KEK in its own process memory would satisfy every
    test above and none of invariant 8. Because the local backend is also the *default*, a
    missing `MOTET_VAULT_BACKEND` in production would silently be that — so real mode
    refuses it outright rather than warning.
    """
    with pytest.raises(VaultConfigError, match="invariant 8"):
        build_key_manager({"MOTET_INFERENCE_MODE": "real"})


def test_the_kms_backend_needs_a_key_name() -> None:
    with pytest.raises(VaultConfigError, match=KMS_KEY_ENV):
        build_key_manager({BACKEND_ENV: "kms", "MOTET_INFERENCE_MODE": "real"})


def test_the_kms_backend_is_selectable_and_does_not_touch_the_network() -> None:
    """Constructing the real manager must not require the SDK or a network.

    The Cloud KMS client is created lazily on first use, which is what lets this class be
    typed, imported, and selected in an environment where the keyring does not exist yet.
    """
    key = build_key_manager(
        {
            BACKEND_ENV: "kms",
            KMS_KEY_ENV: "projects/x/locations/y/keyRings/z/cryptoKeys/k",
            "MOTET_INFERENCE_MODE": "real",
        }
    )
    assert isinstance(key, CloudKmsKeyManager)
    assert key.backend == "kms"
    assert key.key_name.endswith("/cryptoKeys/k")


def test_an_unknown_backend_is_refused() -> None:
    with pytest.raises(VaultConfigError):
        build_key_manager({BACKEND_ENV: "vault"})


def test_a_local_kek_can_be_supplied_and_must_be_the_right_size() -> None:
    supplied = base64.b64encode(b"k" * 32).decode()
    key = build_key_manager({LOCAL_KEK_ENV: supplied})
    assert isinstance(key, LocalKeyManager)
    assert key.kek == b"k" * 32

    with pytest.raises(VaultConfigError, match="AES-256"):
        build_key_manager({LOCAL_KEK_ENV: base64.b64encode(b"short").decode()})
    with pytest.raises(VaultConfigError, match="base64"):
        build_key_manager({LOCAL_KEK_ENV: "not base64 at all!!"})


def test_the_local_kek_is_stable_across_processes() -> None:
    """Two builds agree, so a restart does not orphan every stored credential.

    A random per-process KEK would present as data corruption rather than as "you are on
    the fake backend", which is a far more confusing failure.
    """
    assert build_key_manager({}).key_name == build_key_manager({}).key_name


# --- the encrypt/decrypt split -------------------------------------------------------


def test_the_wrapper_half_satisfies_only_the_encrypt_contract() -> None:
    """Invariant 8's boundary, as far as the type system can carry it.

    ``build_dek_wrapper`` returns the same object narrowed to :class:`DekWrapper`. The
    narrowing is what a route sees, so a change that needed ``unwrap`` would have to alter
    a signature and be visible in review. **IAM is the real control** — the deployed API's
    service account has no KMS decrypt permission — and this is the tripwire in front of it.
    """
    wrapper = build_dek_wrapper({})
    assert isinstance(wrapper, DekWrapper)
    # The static half of the claim: `DekWrapper` has no `unwrap` member, so this assertion
    # is what mypy is checking on every CI run.
    assert "unwrap" not in DekWrapper.__protocol_attrs__  # type: ignore[attr-defined]
    assert "unwrap" in KeyManager.__protocol_attrs__  # type: ignore[attr-defined]
