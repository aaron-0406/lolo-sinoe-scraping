"""Cliente S3 para subir anexos SINOE descargados.

Construye el path siguiendo la convención de Plan §2.6:
  CHB/{customerId}/{chbId}/{clientCode}/case-file/{caseFileId}/binnacle/sinoe/{n_notif}/{tipo}-{ident}.pdf

El path COMPLETO se persiste en `SINOE_NOTIFICATION_ATTACHMENT.s3_key`
(diferencia con `JUDICIAL_BIN_FILE.nameOriginAws` que solo guarda filename).
"""

from __future__ import annotations

from typing import Any

import boto3
import structlog

logger = structlog.get_logger(__name__)

class S3Client:
    def __init__(
        self,
        aws_region: str,
        aws_profile: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        bucket: str = "archivosstorage",
    ) -> None:
        self._region = aws_region
        self._profile = aws_profile
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._bucket = bucket
        self._client: Any = None

    @property
    def bucket(self) -> str:
        return self._bucket

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Si tenemos creds explícitas, las usamos (caso POC: copiadas
            # del backend). Si no, usamos profile/default chain (caso prod
            # con `aws configure --profile viktoria-prod` o IAM role).
            if self._access_key and self._secret_key:
                session = boto3.Session(
                    aws_access_key_id=self._access_key,
                    aws_secret_access_key=self._secret_key,
                    region_name=self._region,
                )
            else:
                session = boto3.Session(
                    profile_name=self._profile, region_name=self._region
                )
            self._client = session.client("s3")
        return self._client

    @staticmethod
    def build_attachment_key(
        *,
        customer_id: int,
        client_code: str,
        case_file_id: int | None,
        n_notificacion: str,
        tipo: str,
        identificacion_anexo: str,
    ) -> str:
        """Construye el S3 key completo. Post-migration backend 20260512100000,
        SINOE es customer-scoped — el path NO incluye chb_id (la notif no
        tiene cartera asociada al insertar). Cuando se matchea con un
        case-file, el segmento `case-file/{id}` se actualiza pero la raíz
        permanece bajo `customer_id`.
        """
        case_segment = f"case-file/{case_file_id}" if case_file_id else "unmatched"
        return (
            f"CHB/{customer_id}/{client_code}/{case_segment}/"
            f"binnacle/sinoe/{n_notificacion}/{tipo}-{identificacion_anexo}.pdf"
        )

    def upload_attachment(
        self,
        *,
        s3_key: str,
        file_bytes: bytes,
        mime_type: str = "application/pdf",
    ) -> None:
        """Upload directo. Si el key ya existe, sobreescribe — la dedupe
        ocurre antes a nivel BD (UNIQUE en SINOE_NOTIFICATION_ATTACHMENT).
        """
        client = self._ensure_client()
        client.put_object(
            Bucket=self._bucket,
            Key=s3_key,
            Body=file_bytes,
            ContentType=mime_type,
            # ServerSideEncryption por default del bucket (AES-256), no
            # se overridea acá. KMS para los archivos sería over-engineering
            # — los anexos son docs públicos del PJ, lo sensible es la cred.
        )
        logger.info(
            "sinoe_attachment_uploaded",
            s3_key=s3_key,
            size_bytes=len(file_bytes),
        )
