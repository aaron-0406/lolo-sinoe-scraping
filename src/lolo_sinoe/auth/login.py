"""SINOE login flow.

Receives an open Page and credentials, performs the login, and returns
a SessionState (cookies + storage state) on success.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from lolo_sinoe.auth.selectors import (
    LOGIN_CAPTCHA_IMG_CANDIDATES,
    LOGIN_CAPTCHA_INPUT_CANDIDATES,
    LOGIN_ERROR_CANDIDATES,
    LOGIN_PASSWORD_CANDIDATES,
    LOGIN_SUBMIT_CANDIDATES,
    LOGIN_URL_PATH,
    LOGIN_USERNAME_CANDIDATES,
    POST_LOGIN_INDICATORS,
)
from lolo_sinoe.captcha.solver import CaptchaSolver
from lolo_sinoe.errors import (
    LoginFailed,
    SinoeUnreachable,
    UnexpectedPageState,
)
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SinoeCredentials:
    casilla: str
    password: str

    def __repr__(self) -> str:
        return f"SinoeCredentials(casilla={self.casilla!r}, password=<redacted>)"


@dataclass
class SessionState:
    cookies: list[Any] = field(default_factory=list)
    storage_state: Any = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    casilla: str = ""
    landed_url: str = ""


async def _first_visible(page: Page, candidates: list[str], timeout_ms: int = 5_000) -> str:
    """Return the first selector from candidates that resolves to a visible element."""
    deadline = timeout_ms
    per_candidate = max(500, deadline // max(1, len(candidates)))
    for sel in candidates:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=per_candidate)
            return sel
        except PlaywrightTimeoutError:
            continue
    raise UnexpectedPageState(
        f"None of {len(candidates)} candidate selectors matched: {candidates[:3]}..."
    )


async def _detect_post_login(page: Page, timeout_ms: int = 10_000) -> str | None:
    """Return the selector that proves we are logged in, or None if not detected."""
    deadline = timeout_ms
    per_candidate = max(500, deadline // max(1, len(POST_LOGIN_INDICATORS)))
    for sel in POST_LOGIN_INDICATORS:
        try:
            await page.wait_for_selector(sel, state="attached", timeout=per_candidate)
            return sel
        except PlaywrightTimeoutError:
            continue
    return None


_DISMISS_MODALS_JS = """
(function() {
  var clicked = 0;
  document.querySelectorAll(
    '.modal [data-dismiss="modal"], .modal .close, .modal .closeP, .modal button.btn-default'
  ).forEach(function(el) { try { el.click(); clicked++; } catch(e) {} });
  document.querySelectorAll('button').forEach(function(b) {
    var t = (b.textContent || '').trim().toUpperCase();
    if (t === 'ACEPTAR' || t === 'CERRAR' || t === 'OK') {
      try { b.click(); clicked++; } catch(e) {}
    }
  });
  document.querySelectorAll('.modal-backdrop, .modal.in, .modal.show, .modal.fade.in')
    .forEach(function(b) { b.style.display = 'none'; b.classList.remove('in', 'show'); });
  document.body.style.overflow = 'auto';
  document.body.classList.remove('modal-open');
  return clicked;
})()
"""


async def _dismiss_modals(page: Page) -> int:
    """Close any open modals/popups. Returns count of buttons clicked."""
    try:
        n = await page.evaluate(_DISMISS_MODALS_JS)
        if n:
            logger.info("modals_dismissed", count=n)
        return int(n)
    except Exception as e:
        logger.warning("modal_dismiss_failed", error=str(e))
        return 0


_SESSION_ACTIVE_TEXT_MARKERS: tuple[str, ...] = (
    "SESIÓN ACTIVA",
    "SESION ACTIVA",
    "sesión activa",
    "sesion activa",
)

_SESSION_ACTIVE_BUTTON_CANDIDATES: list[str] = [
    "button[id$=':btnSalir']",
    "button[title*='Finalizar']",
    "button.btn-sol",
]


async def _is_session_active_page(page: Page) -> bool:
    """True if SINOE rendered the 'sesión activa ya existe' interstitial."""
    try:
        body_text = (await page.locator("body").inner_text(timeout=2_000)).upper()
    except Exception:
        return False
    return any(marker.upper() in body_text for marker in _SESSION_ACTIVE_TEXT_MARKERS)


async def _terminate_other_sessions(page: Page) -> None:
    """Click 'FINALIZAR SESIONES' on the interstitial to free the casilla.

    Per Aaron's explicit instruction (2026-04-30): when SINOE detects a prior
    active session, terminate it automatically rather than aborting the login.

    NOTE: This action redirects to /sso/sso-session-activa.xhtml which renders
    a FRESH login form (not the bandeja). The caller must re-submit credentials.
    """
    button_sel = await _first_visible(page, _SESSION_ACTIVE_BUTTON_CANDIDATES, timeout_ms=5_000)
    logger.info("terminating_other_sessions", selector=button_sel)
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
            await page.click(button_sel)
    except PlaywrightTimeoutError:
        logger.info("terminate_no_navigation", url=page.url)
    # The interstitial logout often re-renders the same modal popup. Dismiss.
    await _dismiss_modals(page)


async def _capture_captcha_image(page: Page, locator: Any) -> bytes:
    """Extrae los bytes del captcha sin pasar por `locator.screenshot()`.

    Razón: `screenshot()` espera a que se carguen TODAS las fuentes de la
    página antes de capturar, y SINOE tiene `@font-face` apuntando a CDNs
    que cuelgan, así que el wait time-outea (~30s).

    Implementación: el `<img>` del captcha YA está cargado en el DOM por
    PrimeFaces (con la sesión JSF activa). Pintamos esa imagen en un
    canvas off-screen y exportamos el PNG en base64 — esto NO depende
    de fonts ni hace una nueva HTTP request al endpoint dinámico (que
    sólo responde dentro del flow del componente JSF).
    """
    import base64

    # Esperá a que la imagen esté loaded antes de leer pixeles.
    await locator.wait_for(state="visible", timeout=10_000)

    js = """
    (img) => new Promise((resolve, reject) => {
      const finish = () => {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth || img.width;
          canvas.height = img.naturalHeight || img.height;
          if (!canvas.width || !canvas.height) {
            reject(new Error('captcha img sin dimensiones'));
            return;
          }
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0);
          // toDataURL devuelve "data:image/png;base64,...."
          resolve(canvas.toDataURL('image/png'));
        } catch (e) {
          reject(e);
        }
      };
      if (img.complete && img.naturalWidth > 0) {
        finish();
      } else {
        img.addEventListener('load', finish, { once: true });
        img.addEventListener('error', () => reject(new Error('img load failed')), { once: true });
        // Safety timeout — JS-side, no Playwright.
        setTimeout(() => reject(new Error('img load timeout')), 8000);
      }
    })
    """
    data_url = await locator.evaluate(js)
    if not data_url or "," not in data_url:
        raise RuntimeError("captcha canvas devolvió data URL inválido")
    return base64.b64decode(data_url.split(",", 1)[1])


async def _is_login_form_present(page: Page, timeout_ms: int = 3_000) -> bool:
    """True if the login form (username input) is currently visible on page."""
    for sel in LOGIN_USERNAME_CANDIDATES:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


async def _detect_error_message(page: Page) -> str | None:
    for sel in LOGIN_ERROR_CANDIDATES:
        loc = page.locator(sel)
        try:
            count = await loc.count()
        except Exception:
            continue
        if count == 0:
            continue
        for i in range(min(count, 3)):
            try:
                if await loc.nth(i).is_visible():
                    text = (await loc.nth(i).inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
    return None


async def login(
    page: Page,
    creds: SinoeCredentials,
    captcha_solver: CaptchaSolver,
    *,
    login_url: str,
    captcha_max_retries: int = 3,
) -> SessionState:
    """Perform a login against SINOE.

    On success returns a SessionState with cookies + storage state.

    Raises:
        SinoeUnreachable: navigation failure / network down.
        LoginFailed: credentials rejected after exhausting captcha retries.
        CaptchaUnsolvable: captcha solver gave up.
        UnexpectedPageState: DOM did not match any expected selector pattern.
    """
    logger.info("login_start", login_url=login_url, casilla=creds.casilla)

    # Retry goto: el primer goto despues de un browser idle del pool tiende
    # a colgar. Un retry rapido despues del timeout suele funcionar (la
    # conexión TLS reusa estado, el segundo intento responde en <2s).
    last_goto_err: Exception | None = None
    for goto_attempt in range(1, 3):
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=15_000)
            last_goto_err = None
            break
        except PlaywrightTimeoutError as e:
            last_goto_err = e
            logger.warning("login_goto_retry", attempt=goto_attempt, error="timeout")
        except Exception as e:
            last_goto_err = e
            logger.warning("login_goto_retry", attempt=goto_attempt, error=str(e)[:120])
    if last_goto_err is not None:
        if isinstance(last_goto_err, PlaywrightTimeoutError):
            raise SinoeUnreachable(f"Could not reach {login_url}: timeout") from last_goto_err
        raise SinoeUnreachable(f"Could not reach {login_url}: {last_goto_err}") from last_goto_err

    if LOGIN_URL_PATH not in page.url:
        logger.warning("login_url_unexpected", current_url=page.url)

    # SINOE shows a welcome / disclaimer modal on every page load (`#dynamicModal`)
    # that intercepts pointer events on the submit button. Dismiss it before doing
    # anything else, and again right before clicking submit (it can re-appear).
    await _dismiss_modals(page)

    last_error: str | None = None

    for attempt in range(1, captcha_max_retries + 1):
        logger.info("login_attempt", attempt=attempt, max=captcha_max_retries)

        username_sel = await _first_visible(page, LOGIN_USERNAME_CANDIDATES)
        password_sel = await _first_visible(page, LOGIN_PASSWORD_CANDIDATES)
        captcha_img_sel = await _first_visible(page, LOGIN_CAPTCHA_IMG_CANDIDATES)
        captcha_input_sel = await _first_visible(page, LOGIN_CAPTCHA_INPUT_CANDIDATES)
        submit_sel = await _first_visible(page, LOGIN_SUBMIT_CANDIDATES)

        logger.debug(
            "login_selectors_resolved",
            username=username_sel,
            password=password_sel,
            captcha_img=captcha_img_sel,
            captcha_input=captcha_input_sel,
            submit=submit_sel,
        )

        await page.fill(username_sel, creds.casilla)
        await page.fill(password_sel, creds.password)

        captcha_locator = page.locator(captcha_img_sel).first
        captcha_bytes = await _capture_captcha_image(page, captcha_locator)
        logger.info("captcha_image_captured", image_bytes=len(captcha_bytes))
        if len(captcha_bytes) == 0:
            raise UnexpectedPageState("captcha img returned 0 bytes")

        # Hard guard: si el provider no responde en 90s, abortar este attempt
        # en vez de hang infinito. La librería 2captcha-python polea con
        # default ~120s sin propagar timeouts asyncio-friendly.
        captcha_solution = await asyncio.wait_for(
            captcha_solver.solve(captcha_bytes), timeout=90.0
        )
        # SINOE captcha is 5 chars and the input has text-transform:uppercase.
        # Normalize to uppercase + strip whitespace to avoid trivial mismatches.
        captcha_normalized = captcha_solution.strip().upper()
        await page.fill(captcha_input_sel, captcha_normalized)

        # Modal can re-appear or be re-rendered after AJAX. Dismiss again before clicking.
        await _dismiss_modals(page)

        # SINOE / PrimeFaces a veces hace AJAX en lugar de full navigation,
        # entonces `expect_navigation` time-outea aunque el login haya
        # andado. El `try` envuelve todo el `async with` porque el
        # TimeoutError sale del `__aexit__`, no de `nav_info.value`.
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=20_000
            ):
                await page.click(submit_sel)
        except PlaywrightTimeoutError:
            logger.info("login_no_navigation", attempt=attempt)

        # JSF puede tardar en re-renderear después del AJAX submit. Damos un
        # margen para que la página se establezca antes de decidir si el
        # login fue exitoso. Sin esto, vemos el form ya removido pero el
        # post-login indicator todavía no presente → falso success.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        await asyncio.sleep(1.0)

        landed_url = page.url
        logger.info("login_post_submit_url", attempt=attempt, url=landed_url)

        # SINOE may render an interstitial warning of an existing active session.
        # Per Aaron's instruction: click FINALIZAR SESIONES, then continue the
        # retry loop (the next page is a FRESH login form on /sso/sso-session-activa.xhtml).
        if await _is_session_active_page(page):
            logger.info("session_active_detected", attempt=attempt)
            await _terminate_other_sessions(page)
            logger.info("session_active_resolved", landed_url=page.url)
            # El click "FINALIZAR SESIONES" deja al user en sso-session-activa.xhtml
            # con un form fresco. Esperar a que el password input este attached
            # antes de seguir — sin esto, la siguiente iteración del loop hace
            # _first_visible y time-outea con UnexpectedPageState porque el
            # form aun no se renderizó por AJAX. Navegamos explicitamente al
            # login.xhtml original para tener un estado consistente.
            #
            # SINOE post-FINALIZAR es lento: 15s no alcanzan en algunas runs y
            # quedaba la página en estado raro. Reintentamos el goto hasta 3
            # veces con timeout más generoso y verificamos que el form de login
            # esté presente antes de continuar.
            relogin_ready = False
            for goto_attempt in range(1, 4):
                try:
                    await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
                    await _dismiss_modals(page)
                    if await _is_login_form_present(page, timeout_ms=5_000):
                        relogin_ready = True
                        logger.info("session_active_relogin_ready", goto_attempt=goto_attempt)
                        break
                except PlaywrightTimeoutError:
                    logger.warning(
                        "session_active_relogin_goto_timeout", goto_attempt=goto_attempt
                    )
                except Exception as e:
                    logger.warning(
                        "session_active_relogin_goto_error",
                        goto_attempt=goto_attempt,
                        error=str(e),
                    )
                await asyncio.sleep(2.0)
            if not relogin_ready:
                logger.warning("session_active_relogin_form_not_ready")
                # No abortamos — el loop sigue, pero esta attempt ya consumió
                # un captcha. Damos chance a las restantes.
            # Don't count this as a captcha attempt — give the user a fresh chance
            # on the new form. The loop will re-fill credentials in the next iteration.
            continue

        # Authoritative success criterion: the login form is no longer present.
        # This works regardless of which URL we land on (bandeja, dashboard, SSO
        # post-step, etc.), and avoids hard-coding URL patterns that may change.
        form_present = await _is_login_form_present(page, timeout_ms=2_000)
        if not form_present:
            indicator = await _detect_post_login(page, timeout_ms=3_000)
            logger.info(
                "login_success",
                attempt=attempt,
                landed_url=page.url,
                indicator=indicator,
            )
            return await _capture_session(page, creds.casilla)

        # Form still present → submit didn't go through. Likely wrong CAPTCHA.
        err = await _detect_error_message(page)
        if err:
            last_error = err
            logger.warning("login_error_message", attempt=attempt, message=err)
            err_lower = err.lower()
            if (
                any(
                    kw in err_lower
                    for kw in (
                        "usuario",
                        "casilla",
                        "contraseña",
                        "password",
                        "credencial",
                        "incorrec",
                    )
                )
                and "captcha" not in err_lower
            ):
                raise LoginFailed(f"Credentials rejected: {err}")

        # Still on login page and no clear credential error: retry with a new captcha.
        # Modal may have re-appeared too. Dismiss before next iteration.
        await _dismiss_modals(page)

    raise LoginFailed(
        f"Login failed after {captcha_max_retries} attempts. Last error: {last_error or 'unknown'}"
    )


async def _capture_session(page: Page, casilla: str) -> SessionState:
    cookies = await page.context.cookies()
    storage_state = await page.context.storage_state()
    return SessionState(
        cookies=cookies,
        storage_state=storage_state,
        captured_at=datetime.now(UTC),
        casilla=casilla,
        landed_url=page.url,
    )
