"""Worker que procesa un job de sync de una cuenta SINOE.

Orquesta el flow completo (Plan §7.2.bis + §6.1 + §6.4):

  1. Claim cuenta (`SELECT ... FOR UPDATE` — race-safe).
  2. Iniciar SINOE_SYNC_LOG (status=running).
  3. Resolver contexto S3 (customer_id, client_code) en UN solo lookup.
  4. Abrir sesión KMS (DEK descifrada UNA vez, reusada para password +
     cached_storage_state).
  5. Aplicar rate limit a SINOE antes de tocar la web.
  6. Intentar reusar `cached_storage_state_blob`. Si SINOE pide login
     de nuevo, full re-login (consume captcha) + persistir nuevo blob.
  7. Delegar al `SyncEngine` el iterate + download + upsert + S3.
  8. Cerrar SINOE_SYNC_LOG con métricas reales.
  9. Errores se clasifican por `isinstance` contra el ENUM del schema
     (ver `errors.error_to_status`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import structlog

from ..auth.login import SinoeCredentials, login
from ..errors import SinoeError, error_to_status
from .shared_resources import SharedResources
from .sync_engine import SyncContext, SyncEngine, SyncMetrics

logger = structlog.get_logger(__name__)

# TTL conservador del storage_state cacheado. SINOE no documenta el
# timeout JSF — empíricamente ~30 min, dejamos 8h porque la sesión sigue
# válida mientras haya actividad. Si SINOE invalida antes, el flow se
# auto-recupera con full re-login (Caso 2 del Plan §6.1.1).
DEFAULT_SESSION_TTL_HOURS = 8


class ScrapeWorker:
    """Procesa un job `{ account_id }` end-to-end.

    Las piezas que requieren acceso a SINOE real (login, navigator,
    download) viven en sus módulos respectivos (`auth/`, `exploration/`,
    `workers/sync_engine`) y se componen acá.
    """

    def __init__(self, resources: SharedResources, worker_id: str) -> None:
        self._r = resources
        self._worker_id = worker_id

    async def process(self, job_data: dict[str, Any]) -> None:
        account_id = int(job_data["account_id"])
        trigger_kind = job_data.get("trigger_kind", "cron")

        # 1. Claim atómico
        account = self._r.accounts.claim_for_sync(account_id)
        if not account:
            logger.warning("sinoe_worker_skip_locked", account_id=account_id)
            return

        # 2. Resolver contexto S3 antes de abrir browser — falla rápido
        customer_id, client_code = self._r.accounts.get_s3_path_context(account.customer_id)

        # 3. SINOE_SYNC_LOG inicial
        sync_log_id = self._r.sync_logs.start(
            account_id=account.id,
            customer_id=account.customer_id,
            trigger_kind=trigger_kind,
            worker_id=self._worker_id,
        )

        metrics = SyncMetrics()
        captcha_solves_consumed = 0
        session_was_reused = False
        status = "ok"
        error_kind: str | None = None
        error_message: str | None = None

        try:
            # 4. KMS: una sola Decrypt(DEK) por sync, reuso para password
            #    + cached_storage_state. El `with` zera la DEK al salir.
            with self._r.kms.open_session(
                encrypted_dek=account.encrypted_dek,
                kms_key_id=account.kms_key_id,
                encryption_context=self._parse_encryption_context(account.encryption_context_json),
            ) as kms_session:
                password_plain = kms_session.decrypt(account.encrypted_password_blob).decode(
                    "utf-8"
                )

                # 5. Cargar storage_state cacheado si existe y no expiró
                cached_state = self._try_load_cached_state(account, kms_session)

                async with self._r.browser_pool.acquire_context(
                    storage_state=cached_state,
                    nav_timeout_ms=self._r.settings.nav_timeout_ms,
                ) as handles:
                    page = handles.page

                    # Probar la sesión cacheada (consume 1 navegación).
                    if cached_state is not None:
                        await self._r.sinoe_rate_limiter.acquire()
                    needs_login = cached_state is None or not await _session_is_alive(page)

                    if not needs_login:
                        session_was_reused = True
                        logger.info(
                            "sinoe_session_reused",
                            account_id=account.id,
                            cached_until=str(account.cached_session_expires_at),
                        )
                    else:
                        if cached_state is not None:
                            logger.info(
                                "sinoe_cached_session_invalid_relogin",
                                account_id=account.id,
                            )
                        await self._r.sinoe_rate_limiter.acquire()
                        creds = SinoeCredentials(
                            casilla=account.casilla_number, password=password_plain
                        )
                        state = await login(
                            page,
                            creds,
                            self._r.captcha_solver,
                            login_url=self._r.settings.login_url,
                            captcha_max_retries=self._r.settings.captcha_max_retries,
                        )
                        captcha_solves_consumed += 1
                        new_blob = json.dumps(state.storage_state).encode("utf-8")
                        encrypted_state = kms_session.encrypt(new_blob)
                        expires_at = datetime.utcnow() + timedelta(hours=DEFAULT_SESSION_TTL_HOURS)
                        self._r.accounts.update_cached_session(
                            account_id=account.id,
                            encrypted_state=encrypted_state,
                            expires_at=expires_at,
                        )

                    # 7. Pipeline de sync (iterate + download + upsert + S3)
                    engine = SyncEngine(
                        page=page,
                        notifications=self._r.notifications,
                        attachments=self._r.attachments,
                        s3_client=self._r.s3,
                        ctx=SyncContext(
                            account_id=account.id,
                            customer_id=customer_id,
                            client_code=client_code,
                        ),
                        sync_log_id=sync_log_id,
                        rate_limiter=self._r.sinoe_rate_limiter,
                    )
                    metrics = await engine.run()

                # Borrar password de memoria asap
                password_plain = "x" * len(password_plain)
                del password_plain

        except Exception as e:
            status = error_to_status(e)
            error_kind = type(e).__name__
            error_message = str(e)[:500]
            # Si falló durante reuse de sesión cacheada, invalidar el cache
            # para que el próximo sync arranque limpio. Evita loops infinitos
            # de "reuse falla → log error → reuse falla otra vez".
            if status in ("session_conflict", "login_failed"):
                try:
                    self._r.accounts.invalidate_cached_session(account_id)
                except Exception:
                    pass
            if isinstance(e, SinoeError):
                logger.warning(
                    "sinoe_worker_failed",
                    account_id=account_id,
                    status=status,
                    error_kind=error_kind,
                )
            else:
                logger.exception(
                    "sinoe_worker_unexpected_error",
                    account_id=account_id,
                    status=status,
                    error_kind=error_kind,
                )

        # 8. Cerrar log + actualizar cuenta
        self._r.sync_logs.finish(
            sync_log_id,
            status=status,
            notifications_seen=metrics.notifications_seen,
            notifications_new=metrics.notifications_new,
            notifications_updated=metrics.notifications_updated,
            attachments_downloaded=metrics.attachments_downloaded,
            attachments_skipped_dedupe=metrics.attachments_skipped_dedupe,
            bytes_downloaded=metrics.bytes_downloaded,
            captcha_solves_consumed=captcha_solves_consumed,
            session_was_reused=session_was_reused,
            error_kind=error_kind,
            error_message=error_message,
        )
        # Las métricas extras (early_stopped, matched_to_case_file, pages_visited)
        # van al log estructurado — son útiles para debug/alertas de comportamiento
        # del scraper, no para la UI de sync log.
        logger.info(
            "sinoe_sync_metrics_extra",
            account_id=account_id,
            sync_log_id=sync_log_id,
            early_stopped=metrics.early_stopped,
            pages_visited=metrics.pages_visited,
            matched_to_case_file=metrics.notifications_matched_to_case_file,
        )
        self._r.accounts.mark_sync_complete(
            account_id,
            status=status,
            notifications_synced_total_increment=metrics.notifications_new,
        )

    @staticmethod
    def _parse_encryption_context(raw: dict[str, Any] | None) -> dict[str, str]:
        """SQLAlchemy con tipo `JSON` deserializa la columna a dict automáticamente.
        Normalizamos las keys/values a strings (KMS los exige así)."""
        if not raw:
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def _try_load_cached_state(self, account: Any, kms_session: Any) -> dict[str, Any] | None:
        """Devuelve el dict de storage_state si hay cache válido y descifrable,
        None si no hay, expiró, o falló el descifrado.

        No raisea — un cache corrupto solo fuerza un re-login, no aborta
        el sync. Side effect: si descifrado falla, invalida el cache
        para que el próximo intento no lo reintente.
        """
        if not account.cached_storage_state_blob:
            return None
        if (
            account.cached_session_expires_at is None
            or account.cached_session_expires_at <= datetime.utcnow()
        ):
            logger.info("sinoe_cached_session_expired", account_id=account.id)
            return None
        try:
            blob_plaintext = kms_session.decrypt(account.cached_storage_state_blob)
            parsed: dict[str, Any] = json.loads(blob_plaintext.decode("utf-8"))
            return parsed
        except Exception as e:
            logger.warning(
                "sinoe_cached_session_decrypt_failed",
                account_id=account.id,
                error=str(e),
            )
            try:
                self._r.accounts.invalidate_cached_session(account.id)
            except Exception:
                pass
            return None


async def _session_is_alive(page: Any) -> bool:
    """Probe rápido: navega a la URL de login. SI estamos logueados, SINOE
    redirige al hub; SI no, queda el form visible. Confiar en
    `_is_login_form_present` (autoritativo) en lugar de URL matching.
    """
    from ..auth.login import _is_login_form_present
    from ..auth.selectors import LOGIN_URL_PATH

    try:
        await page.goto(
            "https://casillas.pj.gob.pe/sinoe/login.xhtml",
            wait_until="domcontentloaded",
        )
        if await _is_login_form_present(page, timeout_ms=2_000):
            return False
        return LOGIN_URL_PATH not in page.url
    except Exception as e:
        logger.warning("sinoe_session_probe_failed", error=str(e))
        return False
