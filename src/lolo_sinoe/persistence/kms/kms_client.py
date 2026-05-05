"""KMS client wrapper para descifrar passwords + storage_state SINOE.

Mirror del backend (`lolo-backend/src/libs/kms.ts`) pero con `decrypt`
(siempre) + `encrypt` (solo para `cached_storage_state_blob`, que el
scraper escribe). El backend NO descifra y el scraper NO cifra passwords —
separación de privilegios IAM (Plan §2.10 + §7.2.bis).

Soporta dos modos:

1. **KMS real**: el backend cifró con `GenerateDataKey` + AES-GCM. Acá
   se reconstruye con `kms.Decrypt(encrypted_dek)` → DEK plaintext →
   AES-GCM decrypt del ciphertext. EncryptionContext debe coincidir.

2. **Fallback POC** (`kms_key_id == "LOCAL:fallback"`): el backend usó
   AES-256-GCM con master key en env. Acá se descifra con la misma key
   del fallback (`SINOE_KMS_FALLBACK_MASTER_KEY`).

   **Rotación dual-key**: si `SINOE_KMS_FALLBACK_MASTER_KEY_OLD` está
   seteada, el `decrypt` intenta primero con la current y si falla con
   la old. El `encrypt` siempre usa la current. Espejo del backend
   (`lolo-backend/src/libs/kms.ts`). Idempotente: ejecutar el endpoint
   `POST /sinoe/accounts/rotate-encryption` del backend re-encripta con
   la current y permite quitar la old del env.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger(__name__)


@dataclass
class EncryptedBlob:
    """Datos persistidos en SINOE_ACCOUNT que el scraper recibe via SQLAlchemy."""

    encrypted_password_blob: bytes
    encrypted_dek: bytes
    kms_key_id: str
    encryption_context: dict[str, str]


class DecryptedSession:
    """Sesión que mantiene el DEK descifrado en memoria para encrypt/decrypt
    de blobs adicionales (cached_storage_state_blob) durante un sync.

    USAR COMO CONTEXT MANAGER — al salir, zera la DEK best-effort
    (Python no garantiza secure-erase, pero acota el scope de exposición).

    Para fallback dual-key se construye con `secondary_dek` opcional. El
    `decrypt` intenta con la primaria y si tira `InvalidTag` reintenta con
    la secundaria. El `encrypt` siempre usa la primaria — los blobs nuevos
    quedan con la key current. Espejo del backend.
    """

    # Layout del ciphertext: iv(12) + authTag(16) + ciphertext.
    # Mismo layout que `lolo-backend/src/libs/kms.ts`. NO romper compat
    # entre repos — el backend cifra password, el scraper cifra storage_state,
    # ambos consumen la misma DEK.
    _IV_LEN = 12
    _TAG_LEN = 16

    def __init__(
        self, dek_plaintext: bytes, secondary_dek: bytes | None = None
    ) -> None:
        if len(dek_plaintext) != 32:
            raise ValueError("DEK debe ser 32 bytes (AES-256)")
        if secondary_dek is not None and len(secondary_dek) != 32:
            raise ValueError("DEK secundaria debe ser 32 bytes (AES-256)")
        self._dek = dek_plaintext
        self._secondary_dek = secondary_dek
        self._aesgcm = AESGCM(dek_plaintext)
        self._aesgcm_secondary = (
            AESGCM(secondary_dek) if secondary_dek is not None else None
        )
        self._closed = False

    def decrypt(self, blob: bytes) -> bytes:
        """Descifra un blob con layout iv(12)+tag(16)+ciphertext.

        Si hay DEK secundaria configurada y la primaria falla con
        `InvalidTag`, reintenta con la secundaria. Si ambas fallan,
        propaga el error de la primaria (es la "current" — debug-friendly).
        """
        if self._closed:
            raise RuntimeError("DecryptedSession ya fue cerrada")
        if len(blob) < self._IV_LEN + self._TAG_LEN:
            raise ValueError(f"blob malformado (< {self._IV_LEN + self._TAG_LEN} bytes)")
        iv = blob[: self._IV_LEN]
        tag = blob[self._IV_LEN : self._IV_LEN + self._TAG_LEN]
        ciphertext = blob[self._IV_LEN + self._TAG_LEN :]
        try:
            return self._aesgcm.decrypt(iv, ciphertext + tag, associated_data=None)
        except Exception as primary_err:
            if self._aesgcm_secondary is None:
                raise
            try:
                return self._aesgcm_secondary.decrypt(
                    iv, ciphertext + tag, associated_data=None
                )
            except Exception:
                # Re-raise el error de la primaria (current) — más útil para
                # debug que "las dos fallaron".
                raise primary_err

    def encrypt(self, plaintext: bytes) -> bytes:
        """Cifra plaintext con la DEK PRIMARIA. Layout iv+tag+ciphertext.

        Solo el scraper invoca esto (para `cached_storage_state_blob`).
        El backend NO debe cifrar storage_state (cada quien su rol).

        Durante una rotación, los blobs nuevos quedan cifrados con la
        current — los blobs viejos cifrados con la old siguen leyéndose
        via fallback hasta que el operador corra el endpoint de rotate.
        """
        if self._closed:
            raise RuntimeError("DecryptedSession ya fue cerrada")
        iv = os.urandom(self._IV_LEN)
        ct_with_tag = self._aesgcm.encrypt(iv, plaintext, associated_data=None)
        # AESGCM.encrypt retorna ciphertext+tag concatenado. Reordenamos al
        # layout del backend: iv + tag + ciphertext.
        ciphertext = ct_with_tag[: -self._TAG_LEN]
        tag = ct_with_tag[-self._TAG_LEN :]
        return iv + tag + ciphertext

    def close(self) -> None:
        if not self._closed:
            # Best effort — Python strings/bytes son inmutables, pero al
            # menos perdemos la referencia.
            self._dek = b"\x00" * len(self._dek)
            if self._secondary_dek is not None:
                self._secondary_dek = b"\x00" * len(self._secondary_dek)
            self._closed = True

    def __enter__(self) -> DecryptedSession:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class KmsClient:
    """Wrapper sobre boto3 KMS + fallback AES-GCM local.

    Se construye una vez al arrancar el server con la región y master key
    de fallback (si aplica) tomadas del config. Luego cada worker llama
    `open_session()` por cada cuenta a sincronizar y descifra/cifra todos
    los blobs (password, cached_storage_state) con la misma DEK.
    """

    def __init__(
        self,
        aws_region: str,
        aws_profile: str | None = None,
        fallback_master_key_b64: str | None = None,
        fallback_master_key_old_b64: str | None = None,
    ) -> None:
        self._fallback_key: bytes | None = None
        self._fallback_key_old: bytes | None = None
        if fallback_master_key_b64:
            self._fallback_key = self._decode_master_key(
                fallback_master_key_b64, "SINOE_KMS_FALLBACK_MASTER_KEY"
            )
        if fallback_master_key_old_b64:
            self._fallback_key_old = self._decode_master_key(
                fallback_master_key_old_b64, "SINOE_KMS_FALLBACK_MASTER_KEY_OLD"
            )

        # Lazy: si nunca se llama con un blob KMS real, no cargamos boto3
        # con creds (útil para tests con solo fallback).
        self._aws_region = aws_region
        self._aws_profile = aws_profile
        self._kms_client: Any = None

    @staticmethod
    def _decode_master_key(b64: str, label: str) -> bytes:
        try:
            buf = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise ValueError(f"{label} no es base64 válido: {e}") from e
        if len(buf) != 32:
            raise ValueError(f"{label} debe ser 32 bytes en base64 (got {len(buf)})")
        return buf

    def is_rotation_active(self) -> bool:
        """True si tanto current como old están seteadas — útil para que
        el `/health` del API server reporte el estado."""
        return self._fallback_key is not None and self._fallback_key_old is not None

    def _ensure_kms_client(self) -> Any:
        if self._kms_client is None:
            session = boto3.Session(profile_name=self._aws_profile, region_name=self._aws_region)
            self._kms_client = session.client("kms")
        return self._kms_client

    def open_session(
        self, encrypted_dek: bytes, kms_key_id: str, encryption_context: dict[str, str]
    ) -> DecryptedSession:
        """Descifra el DEK con KMS (o fallback) y devuelve una sesión
        reusable durante todo el sync de UNA cuenta.

        CloudTrail registra `kms.Decrypt(encrypted_dek)` UNA sola vez por
        sync — el reuse de la DEK para cifrar storage_state es local y no
        deja registro adicional, lo cual es correcto: el evento auditable
        es "el scraper accedió a las creds de la cuenta X".
        """
        if kms_key_id == "LOCAL:fallback":
            if self._fallback_key is None:
                raise RuntimeError(
                    "Blob fue cifrado con LOCAL:fallback pero el scraper no "
                    "tiene SINOE_KMS_FALLBACK_MASTER_KEY configurado."
                )
            # Durante una rotación, `_fallback_key_old` no es None y se
            # pasa como secundaria — `DecryptedSession.decrypt` reintenta
            # con ella si la primaria falla.
            return DecryptedSession(
                self._fallback_key, secondary_dek=self._fallback_key_old
            )

        kms = self._ensure_kms_client()
        resp = kms.decrypt(
            CiphertextBlob=encrypted_dek,
            EncryptionContext=encryption_context,
        )
        return DecryptedSession(resp["Plaintext"])

    def decrypt_password(self, blob: EncryptedBlob) -> str:
        """Conveniencia legacy: abre una sesión, descifra el password,
        cierra la sesión. Para reuse de DEK durante todo el sync, usar
        `open_session()` directamente.
        """
        with self.open_session(
            blob.encrypted_dek, blob.kms_key_id, blob.encryption_context
        ) as session:
            return session.decrypt(blob.encrypted_password_blob).decode("utf-8")
