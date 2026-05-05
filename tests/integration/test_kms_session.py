"""Tests del KMS DecryptedSession — encrypt/decrypt round-trip sin AWS.

Estos tests usan el modo fallback (`LOCAL:fallback` con master key local)
para no requerir credenciales AWS en CI. El path KMS real se valida con
tests `@pytest.mark.live` aparte (requieren `aws kms` configurado).
"""

import pytest

from lolo_sinoe.persistence.kms.kms_client import DecryptedSession, KmsClient


def test_session_encrypt_decrypt_roundtrip(fallback_master_key_b64: str) -> None:
    """Lo cifrado por DecryptedSession.encrypt debe descifrarse por decrypt."""
    client = KmsClient(
        aws_region="us-east-1", fallback_master_key_b64=fallback_master_key_b64
    )
    with client.open_session(
        encrypted_dek=b"unused-in-fallback",
        kms_key_id="LOCAL:fallback",
        encryption_context={"customerHasBankId": "1"},
    ) as session:
        original = b'{"cookies": [{"name": "JSESSIONID", "value": "abc123"}]}'
        encrypted = session.encrypt(original)
        assert encrypted != original
        assert len(encrypted) >= len(original) + 28  # iv(12) + tag(16)
        decrypted = session.decrypt(encrypted)
        assert decrypted == original


def test_session_close_zeroes_dek(fallback_master_key_b64: str) -> None:
    """Tras close(), encrypt/decrypt deben fallar — fail-fast en lugar
    de seguir operando con DEK supuestamente borrada."""
    client = KmsClient(
        aws_region="us-east-1", fallback_master_key_b64=fallback_master_key_b64
    )
    session = client.open_session(
        encrypted_dek=b"unused",
        kms_key_id="LOCAL:fallback",
        encryption_context={},
    )
    session.close()
    with pytest.raises(RuntimeError, match="cerrada"):
        session.encrypt(b"data")
    with pytest.raises(RuntimeError, match="cerrada"):
        session.decrypt(b"\x00" * 28)


def test_session_decrypt_rejects_short_blob() -> None:
    """Blob menor a iv+tag (28 bytes) debe rechazarse antes de tocar AES."""
    import secrets

    session = DecryptedSession(secrets.token_bytes(32))
    with pytest.raises(ValueError, match="malformado"):
        session.decrypt(b"too-short")


def test_session_rejects_non_32_byte_dek() -> None:
    """DEK debe ser AES-256 (32 bytes). Otro tamaño = bug en el caller."""
    with pytest.raises(ValueError, match="AES-256"):
        DecryptedSession(b"\x00" * 16)


def test_two_sessions_independent_iv(fallback_master_key_b64: str) -> None:
    """Cifrar el mismo plaintext dos veces debe dar ciphertext diferente
    (IV aleatorio). Sin esto, ataques de análisis de patrón triviales."""
    client = KmsClient(
        aws_region="us-east-1", fallback_master_key_b64=fallback_master_key_b64
    )
    with client.open_session(b"x", "LOCAL:fallback", {}) as s:
        plaintext = b"same data"
        c1 = s.encrypt(plaintext)
        c2 = s.encrypt(plaintext)
        assert c1 != c2
        assert s.decrypt(c1) == plaintext
        assert s.decrypt(c2) == plaintext
