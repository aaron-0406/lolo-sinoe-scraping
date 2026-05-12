"""SQLAlchemy models — mirror del schema Sequelize del backend.

Mantener sincronizado con `lolo-backend/src/db/models/sinoe-*.model.ts`.
Si se cambia un campo en el schema TS, replicar acá. Los ENUMs viven en
strings literales para no acoplarse a un enum Python (los valores son
los mismos que `lolo-backend/src/app/judicial/constants/sinoe-catalogs.ts`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import ENUM as MysqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SinoeAccount(Base):
    __tablename__ = "SINOE_ACCOUNT"

    id: Mapped[int] = mapped_column("id_sinoe_account", Integer, primary_key=True)
    # Customer-scoped post-migration backend 20260512100000 — la casilla
    # SINOE pertenece al estudio, no a una cartera.
    customer_id: Mapped[int] = mapped_column(
        "customer_id", Integer, nullable=False
    )
    customer_user_id: Mapped[int | None] = mapped_column(
        "customer_user_id_sinoe_account", Integer, nullable=True
    )
    casilla_number: Mapped[str] = mapped_column(String(20), nullable=False)
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_password_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Backend lo persiste como JSON (DataTypes.JSON en Sequelize). MySQL lo
    # guarda en una columna JSON nativa; SQLAlchemy con tipo `JSON` lo
    # deserializa automáticamente a dict al leer.
    encryption_context_json: Mapped[dict[str, Any] | None] = mapped_column(
        "encryption_context_json", JSON, nullable=True
    )
    consent_acceptance_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consent_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consent_acceptor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_frequency_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str] = mapped_column(
        MysqlEnum(
            "ok",
            "login_failed",
            "captcha_unsolvable",
            "sinoe_unreachable",
            "session_conflict",
            "unexpected_dom",
            "partial",
            "never",
        ),
        nullable=False,
        default="never",
    )
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notifications_synced_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_storage_state_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cached_session_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    notifications: Mapped[list[SinoeNotification]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )


class SinoeNotification(Base):
    __tablename__ = "SINOE_NOTIFICATION"

    id: Mapped[int] = mapped_column("id_sinoe_notification", Integer, primary_key=True)
    sinoe_account_id: Mapped[int] = mapped_column(
        "sinoe_account_id_sinoe_notification",
        Integer,
        ForeignKey("SINOE_ACCOUNT.id_sinoe_account"),
        nullable=False,
    )
    customer_id: Mapped[int] = mapped_column(
        "customer_id", Integer, nullable=False
    )
    judicial_case_file_id: Mapped[int | None] = mapped_column(
        "judicial_case_file_id_sinoe_notification", Integer, nullable=True
    )
    n_notificacion: Mapped[str] = mapped_column(String(20), nullable=False)
    sinoe_row_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    n_expediente: Mapped[str] = mapped_column(String(50), nullable=False)
    sumilla: Mapped[str] = mapped_column(Text, nullable=False)
    organo_jurisdiccional: Mapped[str] = mapped_column(String(255), nullable=False)
    fecha_ingreso_casilla: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fecha_surte_efecto: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_inicio_plazo: Mapped[date] = mapped_column(Date, nullable=False)
    estado_lectura_sinoe_at_scrape: Mapped[str] = mapped_column(
        MysqlEnum("leida", "no_leida"), nullable=False
    )
    marked_read_in_viktoria_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_to_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[str] = mapped_column(
        MysqlEnum("low", "medium", "high", "urgent"), nullable=False, default="medium"
    )
    first_seen_in_sync_id: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_in_sync_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped[SinoeAccount] = relationship(back_populates="notifications")
    attachments: Mapped[list[SinoeNotificationAttachment]] = relationship(
        back_populates="notification", cascade="all, delete-orphan"
    )


class SinoeNotificationAttachment(Base):
    __tablename__ = "SINOE_NOTIFICATION_ATTACHMENT"

    id: Mapped[int] = mapped_column("id_sinoe_notification_attachment", Integer, primary_key=True)
    sinoe_notification_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("SINOE_NOTIFICATION.id_sinoe_notification"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo: Mapped[str] = mapped_column(
        MysqlEnum("cedula", "resolucion", "anexo", "escrito", "otro"), nullable=False
    )
    identificacion_anexo: Mapped[str] = mapped_column(String(50), nullable=False)
    numero_paginas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peso_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/pdf")
    tiene_firma_digital: Mapped[bool] = mapped_column(Boolean, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    notification: Mapped[SinoeNotification] = relationship(back_populates="attachments")


class SinoeSyncLog(Base):
    __tablename__ = "SINOE_SYNC_LOG"

    id: Mapped[int] = mapped_column("id_sinoe_sync_log", Integer, primary_key=True)
    sinoe_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("SINOE_ACCOUNT.id_sinoe_account"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        MysqlEnum(
            "ok",
            "login_failed",
            "captcha_unsolvable",
            "sinoe_unreachable",
            "session_conflict",
            "unexpected_dom",
            "partial",
            "running",
        ),
        nullable=False,
    )
    notifications_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notifications_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notifications_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachments_downloaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachments_skipped_dedupe: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    captcha_solves_consumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_was_reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    trigger_kind: Mapped[str] = mapped_column(MysqlEnum("cron", "manual", "retry"), nullable=False)


class SinoeDeadlineAlert(Base):
    __tablename__ = "SINOE_DEADLINE_ALERT_OUTBOX"

    id: Mapped[int] = mapped_column("id_sinoe_deadline_alert", Integer, primary_key=True)
    sinoe_notification_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("SINOE_NOTIFICATION.id_sinoe_notification"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_type: Mapped[str] = mapped_column(
        MysqlEnum(
            "new_notification",
            "deadline_3d",
            "deadline_1d",
            "deadline_today",
            "deadline_overdue",
        ),
        nullable=False,
    )
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    assigned_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
