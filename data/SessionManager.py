from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


TICKETS_HOME_URL = "https://fwc26-shop-mex.tickets.fifa.com/secured/content"
PROFILE_URL = "https://fwc26-shop-mex.tickets.fifa.com/account/editPersonalDetails"
CDP_ENDPOINT = "http://127.0.0.1:9222"
FIFA_HOST = "tickets.fifa.com"


def _url_shows_logged_in_fifa_shop(url: str) -> bool:
    """
    La tienda FIFA mezcla rutas /secured/... (p. ej. content) y /secure/... (p. ej. selection).
    Solo buscar /secured/ deja fuera URLs válidas ya autenticadas.
    """
    if FIFA_HOST not in url:
        return False
    lo = url.lower()
    if "login" in lo:
        return False
    return "/secured/" in url or "/secure/" in url


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class ProfileValidationResult:
    profile_url: str
    household_limit_detected: bool
    daily_restriction_detected: bool
    notes: str


class SessionManager:
    def __init__(
        self,
        session_file: str = "session.json",
        login_timeout_seconds: int = 600,
        cdp_endpoint: str = CDP_ENDPOINT,
    ) -> None:
        self.session_file = Path(session_file)
        self.login_timeout_seconds = login_timeout_seconds
        self.cdp_endpoint = cdp_endpoint

    def capture_session(self) -> dict[str, Any]:
        with sync_playwright() as playwright:
            _log(f"Conectando a Chrome por CDP ({self.cdp_endpoint})...")
            browser = playwright.chromium.connect_over_cdp(self.cdp_endpoint)
            context = self._get_or_create_context(browser)
            page = self._pick_or_prepare_fifa_page(context)

            _log("Esperando sesión autenticada en FIFA (usa la pestaña correcta; el script revisa todas).")
            self._wait_for_manual_login(context)

            _log(f"Guardando storage_state en {self.session_file}...")
            self._save_storage_state(context)

            _log("Abriendo página de perfil para validar reglas de negocio...")
            validation = self.validate_user_profile(page)
            _log("Desconectando Playwright (Chrome sigue abierto).")
            browser.close()

        return {
            "session_file": str(self.session_file),
            "mode": "manual_assist_cdp",
            "cdp_endpoint": self.cdp_endpoint,
            "validation": validation.__dict__,
        }

    def _get_or_create_context(self, browser: Browser) -> BrowserContext:
        if browser.contexts:
            return browser.contexts[0]
        return browser.new_context()

    def _pick_or_prepare_fifa_page(self, context: BrowserContext) -> Page:
        """
        No usar siempre pages[0]: el orden no coincide con la pestaña activa y puede
        quedar en about:blank o en otro sitio mientras FIFA está en otra pestaña.
        """
        pages = list(context.pages)
        for candidate in pages:
            if _url_shows_logged_in_fifa_shop(candidate.url):
                _log(f"Pestaña FIFA detectada: {candidate.url[:80]}...")
                return candidate

        for candidate in pages:
            if FIFA_HOST in candidate.url:
                _log(f"Pestaña FIFA (no aún autenticada): {candidate.url[:80]}...")
                return candidate

        page = context.new_page() if not pages else pages[0]
        if page.url in ("about:blank", "chrome://new-tab-page/", "chrome://newtab/", ""):
            _log(f"Abriendo tienda FIFA en pestaña: {TICKETS_HOME_URL}")
            page.goto(TICKETS_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        else:
            _log(
                "No hay pestaña FIFA aún; usando la primera pestaña del contexto. "
                "Abre manualmente la tienda FIFA en esta ventana o en una nueva pestaña si hace falta."
            )
        return page

    def _any_page_logged_in(self, context: BrowserContext) -> bool:
        for p in context.pages:
            if _url_shows_logged_in_fifa_shop(p.url):
                return True
        return False

    def _first_logged_in_fifa_page(self, context: BrowserContext) -> Page | None:
        for p in context.pages:
            if _url_shows_logged_in_fifa_shop(p.url):
                return p
        return None

    def _poll_pause(self) -> None:
        time.sleep(2)

    def _wait_for_manual_login(self, context: BrowserContext) -> None:
        deadline = time.time() + self.login_timeout_seconds
        while time.time() < deadline:
            if self._any_page_logged_in(context):
                return
            self._poll_pause()
        raise TimeoutError(
            "Login timeout: no se detectó sesión FIFA en ninguna pestaña. "
            "Debe haber una URL de tienda con /secure/... o /secured/... en tickets.fifa.com (y no la pantalla de login)."
        )

    def _save_storage_state(self, context: BrowserContext) -> None:
        context.storage_state(path=str(self.session_file))

    def validate_user_profile(self, page: Page) -> ProfileValidationResult:
        target = page
        logged = self._first_logged_in_fifa_page(page.context)
        if logged is not None:
            target = logged

        target.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            target.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            # The profile page may keep background requests alive. Continue with current DOM.
            pass

        profile_text = target.locator("body").inner_text().lower()
        household_limit_detected = "hogar" in profile_text and "cuatro" in profile_text
        daily_restriction_detected = (
            "partido por dia" in profile_text
            or "partido por día" in profile_text
        )

        notes = (
            "Reglas detectadas en la pagina de perfil."
            if household_limit_detected and daily_restriction_detected
            else "No se detectaron ambas reglas de negocio explicitamente; verificar manualmente en la UI."
        )

        return ProfileValidationResult(
            profile_url=PROFILE_URL,
            household_limit_detected=household_limit_detected,
            daily_restriction_detected=daily_restriction_detected,
            notes=notes,
        )


def main() -> None:
    manager = SessionManager()
    result = manager.capture_session()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
