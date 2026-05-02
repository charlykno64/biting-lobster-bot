from __future__ import annotations

import sys
from typing import Final

from playwright.sync_api import sync_playwright

CDP_DEFAULT: Final[str] = "http://127.0.0.1:9222"
RESTRICTION_BODY_MARKERS: Final[tuple[str, ...]] = (
    "restringido temporalmente",
    "acceso está restringido",
    "acceso esta restringido",
)


def detect_queue_restriction_via_cdp(cdp_endpoint: str = CDP_DEFAULT) -> bool:
    """
    Devuelve True si hay una pestaña CDP en la cola PKP de FIFA y el cuerpo
    indica acceso temporalmente restringido (misma condición que activa
    app.requires_new_chrome_profile en config).
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_endpoint)
            try:
                for ctx in browser.contexts:
                    for page in ctx.pages:
                        url = page.url.lower()
                        if not _url_is_fifa_access_queue(url):
                            continue
                        try:
                            body = page.locator("body").inner_text(timeout=6_000).lower()
                        except Exception:
                            continue
                        if any(m in body for m in RESTRICTION_BODY_MARKERS):
                            return True
            finally:
                browser.close()
    except Exception as exc:
        print(f"chrome_cdp_queue_probe: CDP no disponible o error: {exc}", file=sys.stderr, flush=True)
        return False
    return False


def _url_is_fifa_access_queue(url: str) -> bool:
    if "access.tickets.fifa.com" not in url:
        return False
    return any(m in url for m in ("pkpcontroller/selectqueue", "selectqueue.do"))
