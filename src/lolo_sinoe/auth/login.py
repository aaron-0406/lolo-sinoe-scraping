"""SINOE login flow.

Receives an open Page and credentials, performs the login, and returns
a SessionState (cookies + storage state) on success.
"""

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

    try:
        await page.goto(login_url, wait_until="networkidle")
    except PlaywrightTimeoutError as e:
        raise SinoeUnreachable(f"Could not reach {login_url}: timeout") from e
    except Exception as e:
        raise SinoeUnreachable(f"Could not reach {login_url}: {e}") from e

    if LOGIN_URL_PATH not in page.url:
        logger.warn("login_url_unexpected", current_url=page.url)

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
        captcha_bytes = await captcha_locator.screenshot()
        logger.info("captcha_image_captured", image_bytes=len(captcha_bytes))

        captcha_solution = await captcha_solver.solve(captcha_bytes)
        await page.fill(captcha_input_sel, captcha_solution)

        async with page.expect_navigation(wait_until="networkidle", timeout=20_000) as nav_info:
            await page.click(submit_sel)

        try:
            await nav_info.value
        except PlaywrightTimeoutError:
            logger.info("login_no_navigation", attempt=attempt)

        if LOGIN_URL_PATH not in page.url:
            indicator = await _detect_post_login(page)
            if indicator is not None:
                logger.info(
                    "login_success",
                    attempt=attempt,
                    landed_url=page.url,
                    indicator=indicator,
                )
                return await _capture_session(page, creds.casilla)

        err = await _detect_error_message(page)
        if err:
            last_error = err
            logger.warn("login_error_message", attempt=attempt, message=err)
            err_lower = err.lower()
            if any(
                kw in err_lower
                for kw in ("usuario", "casilla", "contraseña", "password", "credencial", "incorrec")
            ) and "captcha" not in err_lower:
                raise LoginFailed(f"Credentials rejected: {err}")

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
