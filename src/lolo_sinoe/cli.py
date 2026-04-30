"""CLI entry point: lolo-sinoe {login|explore}."""
# ruff: noqa: B008  -- typer.Option in defaults is the standard pattern

import asyncio
import json
import sys
from pathlib import Path

import typer

from lolo_sinoe.auth import SinoeCredentials, login
from lolo_sinoe.browser import launch_browser
from lolo_sinoe.captcha import (
    CapSolverSolver,
    CaptchaSolver,
    FallbackSolver,
    TwoCaptchaSolver,
)
from lolo_sinoe.config import Settings, get_settings
from lolo_sinoe.errors import (
    CaptchaUnsolvable,
    LoginFailed,
    SinoeUnreachable,
    UnexpectedPageState,
)
from lolo_sinoe.exploration.crawler import CrawlConfig, crawl
from lolo_sinoe.exploration.reporter import generate_report
from lolo_sinoe.logging import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _bootstrap() -> Settings:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    return settings


def _build_captcha_solver(settings: Settings) -> CaptchaSolver:
    """Build the CAPTCHA solver chain from settings.

    Honors `captcha_provider_order` (csv) and only includes providers
    whose API key is configured. If only one is configured, returns it
    directly without the FallbackSolver wrapper.
    """
    requested = [p.strip().lower() for p in settings.captcha_provider_order.split(",") if p.strip()]
    available: list[CaptchaSolver] = []
    for provider in requested:
        if provider == "2captcha" and settings.twocaptcha_api_key is not None:
            available.append(
                TwoCaptchaSolver(
                    api_key=settings.twocaptcha_api_key.get_secret_value(),
                    max_retries=settings.captcha_max_retries,
                )
            )
        elif provider == "capsolver" and settings.capsolver_api_key is not None:
            available.append(
                CapSolverSolver(
                    api_key=settings.capsolver_api_key.get_secret_value(),
                    max_retries=settings.captcha_max_retries,
                )
            )
    if not available:
        raise RuntimeError(
            "No CAPTCHA provider configured. Set SINOE_TWOCAPTCHA_API_KEY and/or "
            "SINOE_CAPSOLVER_API_KEY, and ensure SINOE_CAPTCHA_PROVIDER_ORDER includes them."
        )
    if len(available) == 1:
        return available[0]
    return FallbackSolver(available)


@app.command(name="login")
def login_cmd(
    save_session: Path | None = typer.Option(
        None, "--save-session", help="Path to write storage_state JSON on success."
    ),
    headless: bool | None = typer.Option(
        None, "--headless/--no-headless", help="Override SINOE_HEADLESS."
    ),
) -> None:
    """Authenticate against SINOE and print session info."""
    settings = _bootstrap()
    log = get_logger("cli.login")

    use_headless = settings.headless if headless is None else headless

    async def _run() -> int:
        creds = SinoeCredentials(
            casilla=settings.casilla,
            password=settings.password.get_secret_value(),
        )
        solver = _build_captcha_solver(settings)
        log.info("captcha_solver_chain", provider=solver.name)

        async with launch_browser(
            headless=use_headless,
            nav_timeout_ms=settings.nav_timeout_ms,
        ) as h:
            try:
                state = await login(
                    h.page,
                    creds,
                    solver,
                    login_url=settings.login_url,
                    captcha_max_retries=settings.captcha_max_retries,
                )
            except LoginFailed as e:
                log.error("login_failed", error=str(e))
                return 2
            except CaptchaUnsolvable as e:
                log.error("captcha_unsolvable", error=str(e))
                return 3
            except SinoeUnreachable as e:
                log.error("sinoe_unreachable", error=str(e))
                return 4
            except UnexpectedPageState as e:
                log.error("unexpected_page_state", error=str(e))
                return 5

            log.info(
                "login_ok",
                casilla=state.casilla,
                landed_url=state.landed_url,
                cookies=len(state.cookies),
                captured_at=state.captured_at.isoformat(),
            )

            if save_session is not None:
                save_session.parent.mkdir(parents=True, exist_ok=True)
                save_session.write_text(
                    json.dumps(state.storage_state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info("session_saved", path=str(save_session))

            return 0

    exit_code = asyncio.run(_run())
    raise typer.Exit(code=exit_code)


@app.command()
def explore(
    max_pages: int = typer.Option(50, help="Max URLs to visit."),
    max_depth: int = typer.Option(3, help="Max BFS depth from landing page."),
    output_dir: Path = typer.Option(
        Path("exploration_output"), help="Where to write artefacts."
    ),
    headless: bool | None = typer.Option(None, "--headless/--no-headless"),
) -> None:
    """Login and crawl the SINOE authenticated area (read-only)."""
    settings = _bootstrap()
    log = get_logger("cli.explore")

    use_headless = settings.headless if headless is None else headless

    async def _run() -> int:
        creds = SinoeCredentials(
            casilla=settings.casilla,
            password=settings.password.get_secret_value(),
        )
        solver = _build_captcha_solver(settings)
        log.info("captcha_solver_chain", provider=solver.name)

        async with launch_browser(
            headless=use_headless,
            nav_timeout_ms=settings.nav_timeout_ms,
        ) as h:
            try:
                await login(
                    h.page,
                    creds,
                    solver,
                    login_url=settings.login_url,
                    captcha_max_retries=settings.captcha_max_retries,
                )
            except (LoginFailed, CaptchaUnsolvable, SinoeUnreachable, UnexpectedPageState) as e:
                log.error("explore_aborted_login_failed", error=str(e))
                return 2

            log.info("explore_login_ok", landed_url=h.page.url)

            cfg = CrawlConfig(
                max_pages=max_pages,
                max_depth=max_depth,
                output_dir=output_dir,
            )
            result = await crawl(h.page, config=cfg)

            report_path = generate_report(result, output_path=output_dir / "REPORT.md")
            log.info(
                "explore_done",
                visited=len(result.visited),
                skipped=len(result.skipped),
                report=str(report_path),
            )
            return 0

    exit_code = asyncio.run(_run())
    raise typer.Exit(code=exit_code)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
