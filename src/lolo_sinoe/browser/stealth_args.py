"""Chromium launch args.

Ported from lolo-cej-scraping/src/scraping/browser/stealth.config.ts.
Minimal args to avoid bot detection while keeping memory usage low.
"""

STEALTH_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--mute-audio",
]
