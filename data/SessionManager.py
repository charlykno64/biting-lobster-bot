from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from core.camoufox_paths import resolve_camoufox_executable
from core.playwright_proxy import resolve_playwright_proxy


TICKETS_HOME_URL = "https://fwc26-shop-mex.tickets.fifa.com/secured/content"
PROFILE_URL = "https://fwc26-shop-mex.tickets.fifa.com/account/editPersonalDetails"
CDP_ENDPOINT = "http://127.0.0.1:9222"
FIFA_HOST = "tickets.fifa.com"


def _resolve_camoufox_capture_profile_dir(
    hunter_cfg: dict[str, Any],
    app_cfg: dict[str, Any] | None,
) -> Path:
    """
    Orden: hunter.camoufox_capture_profile_dir (override explícito),
    app.biting_lobster_camoufox_profile, luego .camoufox_capture_profile en cwd.
    """
    h = hunter_cfg
    app = app_cfg or {}
    raw = h.get("camoufox_capture_profile_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(os.path.expandvars(raw.strip().strip('"').strip("'"))).expanduser().resolve()
    raw_app = app.get("biting_lobster_camoufox_profile")
    if isinstance(raw_app, str) and raw_app.strip():
        return Path(os.path.expandvars(raw_app.strip().strip('"').strip("'"))).expanduser().resolve()
    return (Path.cwd() / ".camoufox_capture_profile").resolve()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
    return default


def session_capture_wants_console_pause(hunter_cfg: dict[str, Any] | None) -> bool:
    """
    Pausa por input() en consola al detectar cola/captcha (captura CDP y Firefox).
    OR de session_capture_console_pause_on_captcha y camoufox_capture_pause_on_captcha (legado).
    Empaquetado (.exe / instalador): sustituir input() por diálogo en la UI o evento asyncio compartido.
    """
    h = hunter_cfg or {}
    return _coerce_bool(h.get("session_capture_console_pause_on_captcha"), default=False) or _coerce_bool(
        h.get("camoufox_capture_pause_on_captcha"),
        default=False,
    )


def _collect_all_pages(browser: Browser) -> list[Page]:
    """CDP: FIFA puede estar en cualquier pestaña; Chrome puede tener más de un BrowserContext."""
    out: list[Page] = []
    for ctx in list(browser.contexts):
        try:
            out.extend(list(ctx.pages))
        except Exception:
            continue
    return out


def wait_cdp_http_ready(cdp_endpoint: str, timeout_sec: float = 45.0, poll_sec: float = 0.35) -> bool:
    """
    Espera a que el depurador HTTP de Chrome responda (GET .../json/version).
    Útil tras subprocess.Popen(Chrome --remote-debugging-port=9222).
    """
    base = (cdp_endpoint or "").strip().rstrip("/") or CDP_ENDPOINT
    url = f"{base}/json/version"
    deadline = time.time() + max(1.0, float(timeout_sec))
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.5) as resp:
                if resp.status == 200:
                    _ = resp.read(256)
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(max(0.1, float(poll_sec)))
    return False


def _cookie_domain_is_fifa_family(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    if not d:
        return False
    return d.endswith("fifa.com") or d == "fifa.com"


def clear_fifa_family_cookies_in_browser(browser: Browser) -> int:
    """
    Borra cookies cuyo dominio pertenece a la familia FIFA (.fifa.com, .tickets.fifa.com, etc.)
    vía CDP Network.getAllCookies / deleteCookies (no borra otras cookies del perfil).
    """
    removed = 0
    for ctx in list(browser.contexts):
        pages = list(ctx.pages)
        if not pages:
            try:
                page = ctx.new_page()
            except Exception:
                continue
        else:
            page = pages[0]
        try:
            sess = page.context.new_cdp_session(page)
            res = sess.send("Network.getAllCookies")
        except Exception:
            continue
        for c in res.get("cookies", []):
            dom_raw = c.get("domain") or ""
            if not _cookie_domain_is_fifa_family(dom_raw):
                continue
            try:
                payload: dict[str, Any] = {
                    "name": c.get("name"),
                    "domain": c.get("domain"),
                    "path": c.get("path") or "/",
                }
                pk = c.get("partitionKey")
                if pk is not None:
                    payload["partitionKey"] = pk
                sess.send("Network.deleteCookies", payload)
                removed += 1
            except Exception:
                try:
                    sess.send(
                        "Network.deleteCookies",
                        {
                            "name": c.get("name"),
                            "domain": c.get("domain"),
                            "path": c.get("path") or "/",
                        },
                    )
                    removed += 1
                except Exception:
                    pass
    return removed


def navigate_all_pages_about_blank_sync(
    browser: Browser,
    *,
    per_page_timeout_ms: int = 25_000,
    settle_poll_sec: float = 0.2,
    settle_deadline_sec: float = 15.0,
    progress_log: Callable[[str], None] | None = None,
) -> bool:
    """Navega todas las pestañas de todos los contextos CDP a about:blank y espera URLs."""

    def pl(msg: str) -> None:
        _log(msg)
        if progress_log is not None:
            try:
                progress_log(msg)
            except Exception:
                pass

    pages = _collect_all_pages(browser)
    if not pages:
        pl("CDP neutral: sin pestañas visibles; se intenta crear una en el primer contexto.")
        try:
            if browser.contexts:
                pages = [browser.contexts[0].new_page()]
            else:
                pl("CDP neutral: sin contextos; no se puede crear pestaña.")
                return False
        except Exception as exc:
            pl(f"CDP neutral: no se pudo crear pestaña: {exc}")
            return False

    pl(f"CDP neutral: navegando {len(pages)} pestaña(s) a about:blank...")
    for pg in pages:
        try:
            pg.goto("about:blank", wait_until="load", timeout=per_page_timeout_ms)
        except Exception as exc:
            pl(f"CDP neutral: aviso goto about:blank: {exc}")

    deadline = time.time() + max(2.0, float(settle_deadline_sec))
    urls: list[str] = []
    while time.time() < deadline:
        current = _collect_all_pages(browser)
        if not current:
            time.sleep(settle_poll_sec)
            continue
        urls = [(p.url or "").strip().lower() for p in current]
        if all(u == "about:blank" or u == "about:srcdoc" for u in urls):
            pl("CDP neutral: todas las pestañas en about:blank.")
            return True
        time.sleep(settle_poll_sec)

    pl(f"CDP neutral: timeout esperando about:blank; URLs={urls!r}")
    return False


def _cdp_page_url_is_blank_reusable(url: str) -> bool:
    u = (url or "").strip().lower()
    return u in ("about:blank", "about:srcdoc", "")


def _cdp_pick_blank_page_for_reuse(ctx: BrowserContext) -> Page | None:
    """Prioriza la última pestaña en blanco (suele ser la activa o la más reciente)."""
    for pg in reversed(list(ctx.pages)):
        try:
            if _cdp_page_url_is_blank_reusable(pg.url):
                return pg
        except Exception:
            continue
    return None


def open_url_in_cdp_new_tab(
    cdp_endpoint: str,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 90_000,
) -> None:
    """
    Abre `url` en el Chrome ya conectado por CDP (no lanza otro proceso Chrome).
    Si hay una pestaña en about:blank (o vacía), reutiliza esa; si no, crea una pestaña nueva.
    Desconecta Playwright al terminar; el proceso del navegador sigue abierto.
    """
    ep = (cdp_endpoint or "").strip().rstrip("/") or CDP_ENDPOINT
    target = (url or "").strip()
    if not target:
        raise ValueError("url vacia")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ep)
        try:
            if not browser.contexts:
                raise RuntimeError(
                    "Chrome CDP no tiene contextos; pulse «Iniciar Chrome (CDP 9222)» y espere a que arranque."
                )
            ctx = browser.contexts[0]
            reuse = _cdp_pick_blank_page_for_reuse(ctx)
            page = reuse if reuse is not None else ctx.new_page()
            page.goto(target, wait_until=wait_until, timeout=timeout_ms)
        finally:
            browser.close()


def onboarding_cdp_startup_hygiene(
    cdp_endpoint: str = CDP_ENDPOINT,
    *,
    progress_log: Callable[[str], None] | None = None,
) -> tuple[int, list[str]]:
    """
    Tras lanzar Chrome CDP: limpia cookies FIFA en el perfil y deja pestañas en about:blank.
    No cierra el proceso Chrome (solo desconecta Playwright).
    """
    lines: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_endpoint)
        try:
            n = clear_fifa_family_cookies_in_browser(browser)
            lines.append(f"cookies_fifa_eliminadas={n}")
            ok = navigate_all_pages_about_blank_sync(browser, progress_log=progress_log)
            lines.append(f"about_blank_ok={ok}")
            return n, lines
        finally:
            browser.close()


def _url_shows_logged_in_fifa_shop(url: str) -> bool:
    """
    Señal de «listo para guardar sesión de tienda»: no basta con /secure/ genérico ni cola PKP.

    Falsos positivos vistos en Firefox: redirect a access.tickets.fifa.com o rutas /secure/ sin compra;
    la portada /secured/content también.
    """
    if FIFA_HOST not in url:
        return False
    lo = url.lower()
    if "access.tickets.fifa.com" in lo:
        return False
    if "login" in lo or "sign-in" in lo or "signin" in lo:
        return False
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
    except Exception:
        path, query = "", ""
    path_norm = path.rstrip("/") or "/"
    for suffix in ("/secured/content", "/secure/content"):
        if path_norm == suffix or path_norm.endswith(suffix):
            return False

    if "/selection/" in lo:
        return True
    if "/account/" in lo:
        return True
    if "/performance/" in lo:
        return True
    if "/selection/event/seat" in lo:
        return True
    if "perfid=" in query or "perfpid=" in query:
        return True
    return False


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _warn_if_session_looks_tiny(session_file: Path) -> None:
    try:
        sz = session_file.stat().st_size
        if sz < 3500:
            _log(
                f"AVISO: session.json = {sz} bytes (con sesion util suele ser mayor). "
                "Si no alcanzo /selection/ o /account/, reintente la captura."
            )
    except OSError:
        pass


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
        self._capture_progress_fn: Callable[[str], None] | None = None

    def _capture_progress(self, message: str) -> None:
        _log(message)
        fn = self._capture_progress_fn
        if callable(fn):
            try:
                fn(message)
            except Exception:
                pass

    def _chrome_user_agent_sidecar_path(self) -> Path:
        """Junto a session.json: UA del Chrome CDP para alinear Playwright Chromium con el servidor."""
        return self.session_file.parent / f"{self.session_file.stem}_chrome_user_agent.txt"

    def _save_cdp_chrome_user_agent_sidecar(self, page: Page) -> None:
        try:
            ua = page.evaluate("() => navigator.userAgent")
        except Exception as exc:  # noqa: BLE001
            self._capture_progress(f"AVISO: no se pudo leer navigator.userAgent en CDP ({exc}).")
            return
        if not isinstance(ua, str) or not ua.strip():
            self._capture_progress("AVISO: navigator.userAgent vacio en CDP; no se guarda sidecar.")
            return
        path = self._chrome_user_agent_sidecar_path()
        try:
            path.write_text(ua.strip(), encoding="utf-8")
            self._capture_progress(
                f"User-Agent de Chrome CDP guardado para el hunter ({len(ua.strip())} caracteres): {path.name}"
            )
        except OSError as exc:
            self._capture_progress(f"AVISO: no se pudo escribir {path}: {exc}")

    def capture_session(
        self,
        hunter_cfg: dict[str, Any] | None = None,
        *,
        capture_via_ui: bool = False,
        progress_log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self._capture_progress_fn = progress_log
        self._capture_console_wait_on_captcha = session_capture_wants_console_pause(hunter_cfg)
        if self._capture_console_wait_on_captcha and not capture_via_ui:
            self._capture_progress(
                "Captura CDP: session_capture_console_pause_on_captcha activo — "
                "si aparece cola/captcha, la pausa será en la consola/terminal (ENTER)."
            )
        elif self._capture_console_wait_on_captcha and capture_via_ui:
            self._capture_progress(
                "Captura CDP: pausa por input() desactivada al usar el dashboard "
                "(capture_via_ui); solo stderr/CLI. Puede poner session_capture_console_pause_on_captcha: false."
            )
        try:
            with sync_playwright() as playwright:
                self._capture_progress(f"Captura de sesion: conectando por CDP a Chrome ({self.cdp_endpoint})...")
                browser = playwright.chromium.connect_over_cdp(self.cdp_endpoint)
                ctx0 = self._get_or_create_context(browser)
                _ = self._pick_or_prepare_fifa_page(ctx0)
                self._capture_progress(
                    f"CDP: {len(browser.contexts)} contexto(s), {len(_collect_all_pages(browser))} pestaña(s) visibles."
                )

                self._capture_progress(
                    "Validando sesión autenticada en FIFA (revisa todas las pestañas de todos los contextos)."
                )
                self._wait_for_manual_login(browser, capture_via_ui=capture_via_ui)

                ctx_save, page = self._resolve_logged_in_context_and_page(browser)
                if ctx_save is None:
                    ctx_save = self._get_or_create_context(browser)
                    page = self._pick_or_prepare_fifa_page(ctx_save)
                    self._capture_progress(
                        "AVISO: no se encontró URL /selection/ o /account/ en ninguna pestaña; "
                        "se guarda el contexto por defecto (puede fallar validación)."
                    )

                self._capture_progress("Abriendo página de perfil para validar reglas de negocio...")
                validation = self.validate_user_profile(page)

                self._save_cdp_chrome_user_agent_sidecar(page)

                self._capture_progress(f"Guardando storage_state en {self.session_file}...")
                self._save_storage_state(ctx_save)
                _warn_if_session_looks_tiny(self.session_file)

                if _coerce_bool((hunter_cfg or {}).get("attach_hunter_to_chrome_cdp"), default=False):
                    self._capture_progress(
                        "Modo attach_hunter_to_chrome_cdp: se omiten pestañas about:blank post-captura "
                        "(el hunter reutilizara esta ventana; no se vacia el DOM FIFA)."
                    )
                else:
                    self._capture_progress(
                        "CDP neutral: pestañas a about:blank tras session.json "
                        "(descarga DOM FIFA en Chrome; reduce scripts/telemetría en paralelo al hunter)."
                    )
                    navigate_all_pages_about_blank_sync(browser, progress_log=self._capture_progress)

                self._capture_progress("Desconectando Playwright (Chrome sigue abierto).")
                browser.close()

                return {
                    "session_file": str(self.session_file),
                    "mode": "manual_assist_cdp",
                    "cdp_endpoint": self.cdp_endpoint,
                    "validation": validation.__dict__,
                }

        except Exception as exc:
            low = str(exc).lower()
            if "has been closed" in low or "target closed" in low:
                raise RuntimeError(
                    "Chrome CDP cerro o perdio la sesion antes de guardar session.json. "
                    "Vuelva a abrir «Iniciar Chrome (CDP 9222)», no cierre la ventana hasta terminar la captura; "
                    "si Playwright (hunter) estaba abierto, ciérrelo antes — puede interferir con el mismo puerto o perfil."
                ) from exc
            raise
        finally:
            self._capture_progress_fn = None

    def capture_session_firefox(
        self,
        hunter_cfg: dict[str, Any] | None = None,
        app_cfg: dict[str, Any] | None = None,
        *,
        capture_via_ui: bool = False,
        progress_log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """
        Abre Firefox (Camoufox si hunter.camoufox_executable / CAMOUFOX_PATH) con Playwright,
        espera login manual en la tienda y guarda session.json (mismo formato que CDP).

        Perfil en disco (modo no efímero): hunter.camoufox_capture_profile_dir si está definido;
        si no, app.biting_lobster_camoufox_profile; si no, .camoufox_capture_profile en cwd.

        No usa puerto 9222: la ventana es hija del proceso de captura hasta que termina.
        """
        h = hunter_cfg or {}
        app = app_cfg or {}
        exe_path: Path | None = resolve_camoufox_executable(h)
        exe_label: str
        raw_slow = h.get("camoufox_capture_slow_mo_ms", 0)
        try:
            slow_mo = max(0, int(raw_slow))
        except (TypeError, ValueError):
            slow_mo = 0

        firefox_user_prefs: dict[str, str | bool | int] = {"dom.webdriver.enabled": False}
        if _coerce_bool(h.get("camoufox_disable_coop"), default=False):
            firefox_user_prefs["browser.tabs.remote.useCrossOriginOpenerPolicy"] = False
            firefox_user_prefs["browser.tabs.remote.useCrossOriginEmbedderPolicy"] = False
        extra_prefs = h.get("camoufox_capture_firefox_prefs")
        if isinstance(extra_prefs, dict):
            for k, v in extra_prefs.items():
                firefox_user_prefs[str(k)] = v

        if _coerce_bool(h.get("camoufox_enable_humanize"), default=False):
            slow_mo = max(90, slow_mo)
            _log("Camoufox: enable_humanize activo para captura (slow_mo>=90ms).")

        proxy = resolve_playwright_proxy(h)
        if proxy is not None:
            _log(f"Captura Firefox: proxy activo ({proxy.get('server')}).")
        ignore_https_errors = _coerce_bool(h.get("camoufox_ignore_https_errors"), default=False)
        if ignore_https_errors:
            _log("Camoufox: ignore_https_errors=true para captura (proxy TLS con CA no confiada).")
        self._capture_console_wait_on_captcha = session_capture_wants_console_pause(h)

        profile_dir = _resolve_camoufox_capture_profile_dir(h, app)
        profile_dir.mkdir(parents=True, exist_ok=True)

        use_ephemeral = bool(h.get("camoufox_capture_ephemeral", False))

        self._capture_progress_fn = progress_log
        try:
            with sync_playwright() as playwright:
                viewport = {"width": 1680, "height": 900}
                raw_vp = h.get("playwright_viewport")
                if isinstance(raw_vp, dict):
                    try:
                        w = int(raw_vp.get("width", viewport["width"]))
                        hg = int(raw_vp.get("height", viewport["height"]))
                        viewport = {"width": max(1024, w), "height": max(600, hg)}
                    except (TypeError, ValueError):
                        pass
                auto_fp_screen = _coerce_bool(h.get("camoufox_auto_fingerprint_screen"), default=True)
                if auto_fp_screen:
                    _log("Camoufox: auto_fingerprint_screen activo (captura sin viewport fijo).")

                if exe_path is not None:
                    exe_label = str(exe_path)
                    _log(f"Captura Firefox: ejecutable {exe_label}")
                else:
                    exe_label = "playwright-bundled-firefox"
                    _log(
                        "Captura Firefox: Firefox embebido Playwright "
                        "(`python -m playwright install firefox`). Camoufox: hunter.camoufox_executable."
                    )

                if use_ephemeral:
                    _log(
                        "Captura Firefox: modo efimero (camoufox_capture_ephemeral=true). "
                        "Si el boton del captcha no responde, pruebe modo persistente (quitar esa clave)."
                    )
                    launch_kw: dict[str, Any] = {
                        "headless": False,
                        "firefox_user_prefs": firefox_user_prefs,
                        "slow_mo": slow_mo,
                    }
                    if proxy is not None:
                        launch_kw["proxy"] = proxy
                    if exe_path is not None:
                        launch_kw["executable_path"] = str(exe_path)
                    browser = playwright.firefox.launch(**launch_kw)
                    ctx_kw: dict[str, Any] = {
                        "locale": "es-MX",
                        "ignore_https_errors": ignore_https_errors,
                    }
                    if not auto_fp_screen:
                        ctx_kw["viewport"] = viewport
                    context = browser.new_context(**ctx_kw)
                    page = context.new_page()
                    closer = lambda: browser.close()
                else:
                    _log(
                        f"Captura Firefox: perfil persistente {profile_dir} "
                        "(cookies y estado se conservan entre capturas; borre la carpeta para empezar limpio)."
                    )
                    pc_kw: dict[str, Any] = {
                        "headless": False,
                        "locale": "es-MX",
                        "ignore_https_errors": ignore_https_errors,
                        "firefox_user_prefs": firefox_user_prefs,
                        "slow_mo": slow_mo,
                    }
                    if not auto_fp_screen:
                        pc_kw["viewport"] = viewport
                    if proxy is not None:
                        pc_kw["proxy"] = proxy
                    if exe_path is not None:
                        pc_kw["executable_path"] = str(exe_path)
                    context = playwright.firefox.launch_persistent_context(str(profile_dir), **pc_kw)
                    page = context.pages[0] if context.pages else context.new_page()
                    closer = lambda: context.close()

                _log(
                    "Captura Firefox: NO carga session.json previo; al final SOBREESCRIBE session.json. "
                    "DataDome: haga clic dentro del recuadro del captcha antes de rellenar; pruebe Enter tras resolver; "
                    "zoom 100%. Si el envio no hace nada, use captura Chrome CDP para este paso o borre la carpeta de perfil y reintente."
                )
                _log(f"Abriendo tienda FIFA; tope {self.login_timeout_seconds}s.")
                page.goto(TICKETS_HOME_URL, wait_until="domcontentloaded", timeout=90_000)
                _log(
                    "Inicie sesion y avance hasta /selection/... o /account/... (ver mensajes de espera). "
                    "Portada /secured/content sola no guarda."
                )

                self._wait_for_manual_login(context, capture_via_ui=capture_via_ui)

                _log(f"Guardando storage_state en {self.session_file}...")
                self._save_storage_state(context)
                _warn_if_session_looks_tiny(self.session_file)

                _log("Abriendo página de perfil para validar reglas de negocio...")
                validation = self.validate_user_profile(page)

                _log("Captura terminada: cerrando ventana Firefox/Camoufox (el guardado ya se hizo).")
                closer()

        except Exception as exc:
            low = str(exc).lower()
            if "has been closed" in low or "target closed" in low:
                raise RuntimeError(
                    "Firefox de captura cerro antes de guardar session.json. "
                    "No cierre la ventana hasta que el mensaje OK aparezca en el log."
                ) from exc
            raise
        finally:
            self._capture_progress_fn = None

        return {
            "session_file": str(self.session_file),
            "mode": "firefox_manual_playwright",
            "executable": exe_label,
            "validation": validation.__dict__,
        }

    def _get_or_create_context(self, browser: Browser) -> BrowserContext:
        if browser.contexts:
            return browser.contexts[0]
        return browser.new_context()

    def _resolve_logged_in_context_and_page(self, browser: Browser) -> tuple[BrowserContext | None, Page | None]:
        """Contexto que tiene una pestaña con URL de tienda lista para session.json."""
        for ctx in list(browser.contexts):
            try:
                for p in list(ctx.pages):
                    if _url_shows_logged_in_fifa_shop(p.url):
                        return ctx, p
            except Exception:
                continue
        return None, None

    def _iter_pages_for_wait(self, scope: Union[Browser, BrowserContext]) -> list[Page]:
        if isinstance(scope, Browser):
            return _collect_all_pages(scope)
        return list(scope.pages)

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
        _blank_hosts = (
            "about:blank",
            "about:newtab",
            "about:privatebrowsing",
            "chrome://new-tab-page/",
            "chrome://newtab/",
            "",
        )
        if page.url in _blank_hosts:
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

    @staticmethod
    def _looks_like_queue_or_captcha_url(url: str) -> bool:
        """
        Cola PKP / captcha. Evitar 'queue=' suelto en shop FIFA (falsos positivos y input() bloqueando la UI).
        """
        lo = (url or "").lower()
        if "access.tickets.fifa.com" in lo or "pkpcontroller" in lo:
            return True
        if "captcha-delivery.com" in lo or "geo.captcha-delivery.com" in lo:
            return True
        if "queue-it.net" in lo or "queueit.net" in lo:
            return True
        if "queue=" in lo and ("access.tickets" in lo or "pkpcontroller" in lo or "queue-it" in lo):
            return True
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host.endswith("datadome.co") or host == "datadome.co":
            return True
        return False

    def _wait_after_manual_captcha_step(self, scope: Union[Browser, BrowserContext]) -> None:
        """Tras continuar desde consola: esperar carga estable antes de seguir el bucle."""
        for p in self._iter_pages_for_wait(scope):
            try:
                p.wait_for_load_state("domcontentloaded", timeout=90_000)
            except PlaywrightTimeoutError:
                pass
            try:
                p.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
        time.sleep(0.35)

    def _wait_for_manual_login(self, scope: Union[Browser, BrowserContext], *, capture_via_ui: bool = False) -> None:
        deadline = time.time() + self.login_timeout_seconds
        last_hb = 0.0
        console_wait = _coerce_bool(getattr(self, "_capture_console_wait_on_captcha", False), default=False)
        while time.time() < deadline:
            pages = self._iter_pages_for_wait(scope)
            for p in pages:
                if _url_shows_logged_in_fifa_shop(p.url):
                    self._capture_progress(
                        f"Sesion tienda OK segun URL: {p.url[:200]}{'...' if len(p.url) > 200 else ''}"
                    )
                    return
            if console_wait and any(self._looks_like_queue_or_captcha_url(p.url) for p in pages):
                if capture_via_ui or not sys.stdin.isatty():
                    self._capture_progress(
                        "Captcha/cola detectado: pausa consola omitida (dashboard o stdin no TTY). "
                        "Resuelva en Chrome; para ENTER en terminal use CLI o capture_via_ui=false desde IDE."
                    )
                    self._wait_after_manual_captcha_step(scope)
                    continue
                self._capture_progress(
                    "Captcha/cola detectado: pausa en consola (ENTER). No use el boton del dashboard hasta continuar aqui."
                )
                # Empaquetado: reemplazar por diálogo Flet/snackbar + threading.Event o cola async.
                input("🚨 Resuelve el CAPTCHA manualmente y luego presiona ENTER aquí para continuar...")
                self._wait_after_manual_captcha_step(scope)
                continue
            now = time.time()
            if now - last_hb > 45:
                try:
                    snap = [f"{getattr(x, 'url', '')[:120]}" for x in pages]
                except Exception:
                    snap = ["(sin url)"]
                self._capture_progress(
                    "Esperando login / flujo tienda... URLs actuales: "
                    f"{snap} — avance hasta lista de fechas/producto (/selection/) o Mi cuenta (/account/)."
                )
                last_hb = now
            self._poll_pause()
        raise TimeoutError(
            "Login timeout: no se detecto flujo de tienda listo en ninguna pestaña. "
            "Necesita URL con /selection/... o /account/ en fwc26-shop-*.tickets.fifa.com "
            "(/secure/... o /secured/...), sin estar solo en access.tickets.fifa.com (cola) "
            "ni solo en la portada /secured/content."
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
