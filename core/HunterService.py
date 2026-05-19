from __future__ import annotations

import asyncio
import inspect
import math
import random
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Frame, Locator, Page, Playwright, Route, async_playwright

SeatUiRoot = Page | Frame
from playwright_stealth import Stealth

from core.currency import CurrencyConverter
from core.camoufox_paths import resolve_camoufox_executable
from core.hunter_prereqs import validate_hunter_search_objective
from core.team_mapping import resolve_team_country_name
from core.playwright_proxy import (
    playwright_ignore_https_errors_from_cfg,
    resolve_playwright_proxy,
)
from data.SessionManager import _url_shows_logged_in_fifa_shop

FIFA_HOST = "tickets.fifa.com"
DEFAULT_SHOP_HOST = "https://fwc26-shop-mex.tickets.fifa.com"
DEFAULT_PRODUCT_ID = "10229225515651"
_DATE_SELECTION_RE = re.compile(
    r"https?://[^/]*tickets\.fifa\.com/(?:secure|secured)/selection/event/date",
    re.I,
)
_SEAT_STEP_RE = re.compile(r"/(?:secure|secured)/selection/event/seat", re.I)
_PERF_ID_IN_URL_RE = re.compile(r"/performance/(\d+)", re.I)
_PERF_ID_QUERY_RE = re.compile(r"[?&]perfId=(\d+)", re.I)
_RE_ARIA_EVENT_CODE_M = re.compile(r"event-code-M(\d+)", re.I)
_RE_ARIA_EVENT_CODE_NUM = re.compile(r"event-code-(\d+)", re.I)
_RE_ARIA_TEAMS_M_TOKEN = re.compile(r"\b(teams_M\d+)\b", re.I)

# Siguen pasando: stylesheet, font, image, script (FIFA/DataDome), xhr/fetch.
_DEFAULT_PROXY_ROUTE_BLOCKED_URL_SUBSTRINGS: tuple[str, ...] = (
    "google-analytics.com",
    "analytics.tiktok.com",
    "connect.facebook.net",
    "googletagmanager.com",
    "hotjar.com",
)
_SPEED_BOUNDS_SEC: dict[str, tuple[float, float]] = {
    "alta": (0.250, 0.499),
    "media": (0.500, 1.299),
    "baja": (1.000, 2.200),
}
_SPEED_ALIASES: dict[str, str] = {"high": "alta", "medium": "media", "low": "baja"}

EventCallback = Callable[[str, dict[str, Any]], Any]


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


class SeatFlowBlockedByCaptchaError(Exception):
    """DataDome u otro captcha bloquea la vista de asientos; reintentar el listado en headless no ayuda."""


class SeatFlowNaturalEntryError(Exception):
    """No se logró una entrada natural desde el listado (sin saltos agresivos a tabla)."""


class HunterService:
    """
    Cacería sin CDP: Chromium headless + session.json + playwright-stealth (apply_stealth_async tras new_page,
    antes del primer goto). Chromium: --disable-blink-features=AutomationControlled, sin --enable-automation por defecto;
    User-Agent y viewport 1920x1080 alineados con Chrome CDP si existe session_chrome_user_agent.txt (captura CDP).

    Entrada **natural** (por defecto): `/secured/content` → clic COMPRAR BOLETOS →
    pantalla de fechas; luego, si la URL no es aún la canónica del listado SSR,
    `goto(match_list_url())`. Así el flujo se parece al usuario y el DOM queda
    alineado con la URL `/secure/.../date/product/<id>/lang/<lang>`.

    Con `hunter.skip_secured_content: true` o `open_secured_content_tienda: false` (defecto)
    se abre directamente `match_list_url()` con pausa de 10 s (sin `/secured/content` + COMPRAR BOLETOS).

    Con `hunter.team_filter_probe_only: true` el run termina tras filtro #team,
    resolucion p#teams_MNN (fila por target_teams + p[id^=teams_M] en DOM; opcional `team_filter_probe_teams_paragraph_id`),
    clic en el contenedor del partido (`li` ancestro) con `bezier_human_click` o `locator.click`
    (`team_filter_probe_match_open_playwright_click`);
    luego opcional `team_filter_probe_post_match_console_gate` (ENTER pre-ceguera), ceguera post_navigation_blind_*,
    reubicacion del puntero; Fase 2: pausas cortas, li#tab-2-link y a#book en main con bezier_human_click.

    Tras elegir partido, por defecto se hace `goto` a
    `.../seat/performance/<perfId>/table/<seat_table_index>/lang/...` para evitar
    el mapa lento; con `hunter.use_seat_map_entry: true` se usa el clic del listado
    (flujo con mapa + botón Mejor sitio).

    Con `hunter.seat_entry_via_tab_link: true` (sin mapa) se hace clic en la fila del
    partido desde el listado (vista asientos), luego `li#tab-2-link`; solo si falta el tab
    se prueba goto al shell `?productId&perfId&table=` y por último goto directo `/table/N`.

    Solo `wait_until=\"domcontentloaded\"` en goto/reload — nunca networkidle.
    """

    def __init__(
        self,
        project_root: Path,
        config: dict[str, Any],
        *,
        session_file: str = "session.json",
        on_event: EventCallback | None = None,
        debug_continue_event: asyncio.Event | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.config = config
        self.session_path = self.project_root / session_file
        self._on_event = on_event
        self._debug_continue_event = debug_continue_event
        self._stop = asyncio.Event()
        self._match_list_diagnostic_emitted = False
        self._last_seat_ui_root: SeatUiRoot | None = None
        self._active_browser: Browser | None = None
        self._active_context: BrowserContext | None = None
        self._active_page: Page | None = None
        self._chromium_stealth_plugin: Stealth | None = None
        self._last_bezier_click_viewport: tuple[float, float] | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def _sync_playwright_session_refs(self, browser: Browser | None, context: BrowserContext | None, page: Page | None) -> None:
        self._active_browser = browser
        self._active_context = context
        self._active_page = page

    def _playwright_headless_launch_arg(self) -> bool:
        """True = Chromium sin ventana; False = ventana Playwright visible (hunter.playwright_headless: false)."""
        return bool(self._hunter_cfg().get("playwright_headless", True))

    def _use_camoufox(self) -> bool:
        """Segundo backend: Firefox Camoufox (huella distinta a Chromium; mejor para DataDome en algunos casos)."""
        return bool(self._hunter_cfg().get("use_camoufox", False))

    def _hunter_attach_chrome_cdp(self) -> bool:
        """True: hunter usa connect_over_cdp al Chrome CDP ya abierto (sin launch Chromium ni new_context)."""
        if self._use_camoufox():
            return False
        return _coerce_bool(self._hunter_cfg().get("attach_hunter_to_chrome_cdp"), default=False)

    def _resolve_camoufox_executable(self) -> Path | None:
        return resolve_camoufox_executable(self._hunter_cfg())

    def _playwright_ignore_https_errors(self) -> bool:
        return playwright_ignore_https_errors_from_cfg(self._hunter_cfg())

    def _camoufox_auto_fingerprint_screen(self) -> bool:
        return _coerce_bool(self._hunter_cfg().get("camoufox_auto_fingerprint_screen"), default=True)

    def _chromium_context_proxy_kw(self) -> dict[str, Any]:
        """Repite proxy en new_context (algunos Chromium/Playwright arreglan CONNECT con auth)."""
        if self._use_camoufox():
            return {}
        px = resolve_playwright_proxy(self._hunter_cfg())
        if px is None:
            return {}
        return {"proxy": px}

    async def _launch_playwright_browser(self, p: Playwright) -> Browser:
        """Chromium (por defecto) o Firefox Camoufox si hunter.use_camoufox: true."""
        headless = self._playwright_headless_launch_arg()
        if self._use_camoufox():
            exe = self._resolve_camoufox_executable()
            if exe is None:
                raise RuntimeError(
                    "hunter.use_camoufox=true pero no se encontro el ejecutable. "
                    "Defina hunter.camoufox_executable en config.yaml o la variable de entorno CAMOUFOX_PATH "
                    "(ruta al .exe de Camoufox en Windows)."
                )
            self._emit(
                "log",
                {"message": f"Playwright: lanzando Camoufox (Firefox) desde {exe}"},
            )
            launch_kw: dict[str, Any] = {
                "headless": headless,
                "executable_path": str(exe),
            }
            if _coerce_bool(self._hunter_cfg().get("camoufox_disable_coop"), default=False):
                launch_kw["firefox_user_prefs"] = {
                    "browser.tabs.remote.useCrossOriginOpenerPolicy": False,
                    "browser.tabs.remote.useCrossOriginEmbedderPolicy": False,
                }
                self._emit("log", {"message": "Camoufox: COOP/COEP relajado (camoufox_disable_coop=true)."})
            proxy = resolve_playwright_proxy(self._hunter_cfg())
            if proxy is not None:
                launch_kw["proxy"] = proxy
                self._emit("log", {"message": f"Camoufox: proxy activo ({proxy.get('server')})."})
            if _coerce_bool(self._hunter_cfg().get("camoufox_enable_humanize"), default=False):
                self._emit(
                    "log",
                    {"message": "Camoufox: enable_humanize=true (el hunter ya aplica clicks/tiempos humanizados)."},
                )
            return await p.firefox.launch(**launch_kw)
        launch_ch: dict[str, Any] = {
            "headless": headless,
            "ignore_default_args": ["--enable-automation"],
        }
        proxy_ch = resolve_playwright_proxy(self._hunter_cfg())
        if proxy_ch is not None:
            launch_ch["proxy"] = proxy_ch
        extra_args: list[str] = ["--disable-blink-features=AutomationControlled"]
        if self._playwright_ignore_https_errors():
            extra_args.extend(["--ignore-certificate-errors", "--test-type"])
        launch_ch["args"] = extra_args
        return await p.chromium.launch(**launch_ch)

    def _playwright_viewport_size(self) -> dict[str, int]:
        """
        Viewport del contexto Playwright. Ancho generoso por defecto: en FIFA el botón «Seleccionar»
        a veces desaparece por media queries cuando el ancho es pequeño (layout móvil / fila sola).
        """
        h = self._hunter_cfg()
        raw = h.get("playwright_viewport")
        w = hg = None
        if isinstance(raw, dict):
            w = raw.get("width")
            hg = raw.get("height")
        if w is None:
            w = h.get("playwright_viewport_width")
        if hg is None:
            hg = h.get("playwright_viewport_height")
        try:
            width = int(w) if w is not None else 1680
        except (TypeError, ValueError):
            width = 1680
        try:
            height = int(hg) if hg is not None else 900
        except (TypeError, ValueError):
            height = 900
        return {"width": max(1024, width), "height": max(600, height)}

    def _chrome_cdp_user_agent_sidecar_path(self) -> Path:
        """Par session.json: UA guardado en captura CDP (SessionManager)."""
        return self.session_path.parent / f"{self.session_path.stem}_chrome_user_agent.txt"

    def _load_chrome_cdp_saved_user_agent(self) -> str | None:
        path = self._chrome_cdp_user_agent_sidecar_path()
        if not path.is_file():
            return None
        try:
            s = path.read_text(encoding="utf-8").strip()
            return s if s else None
        except OSError:
            return None

    def _chromium_stealth_viewport(self) -> dict[str, int]:
        """
        Viewport de escritorio estándar para contexto Chromium (evasión headless / tamaño raro).
        Override: hunter.chromium_stealth_viewport: {width, height}
        """
        h = self._hunter_cfg()
        raw = h.get("chromium_stealth_viewport")
        if isinstance(raw, dict):
            try:
                w = int(raw.get("width", 1920))
                hg = int(raw.get("height", 1080))
                return {"width": max(1024, w), "height": max(600, hg)}
            except (TypeError, ValueError):
                pass
        return {"width": 1920, "height": 1080}

    async def _apply_chromium_stealth_to_page(self, page: Page) -> None:
        """playwright-stealth: init scripts en la página antes del primer goto (Chromium hunter / reopen)."""
        if self._use_camoufox():
            return
        plug = self._chromium_stealth_plugin
        if plug is None:
            return
        await plug.apply_stealth_async(page)

    def _match_list_min_viewport_width(self) -> int:
        raw = self._hunter_cfg().get("match_list_min_viewport_width", 1400)
        try:
            return max(1024, int(raw))
        except (TypeError, ValueError):
            return 1400

    async def _ensure_desktop_viewport_for_match_list(self, page: Page) -> None:
        """Si el ancho es bajo, amplía viewport antes de interactuar con el listado (CTA responsive)."""
        target = self._playwright_viewport_size()
        min_w = self._match_list_min_viewport_width()
        try:
            cur = page.viewport_size
        except Exception:
            cur = None
        if cur is None or int(cur.get("width") or 0) < min_w:
            await page.set_viewport_size(target)
            self._emit(
                "log",
                {
                    "message": (
                        f"Viewport {target['width']}x{target['height']} (umbral {min_w}px): "
                        "evita que «Seleccionar» quede oculto por CSS en ventanas estrechas."
                    ),
                },
            )
            await asyncio.sleep(0.35)

    async def _mouse_click_viewport_center_of(self, page: Page, loc: Locator, *, label: str) -> bool:
        """Clic con puntero real en el centro del elemento (coordenadas de viewport)."""
        try:
            if await loc.count() == 0:
                return False
            box = await loc.first.bounding_box()
            if box is None:
                self._emit("log", {"message": f"Abrir partido: {label} sin bounding_box en viewport."})
                return False
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y)
            await asyncio.sleep(0.05)
            await page.mouse.click(x, y)
            self._emit("log", {"message": f"Abrir partido: {label} (page.mouse ~{x:.0f},{y:.0f})"})
            return True
        except Exception as exc:
            self._emit("log", {"message": f"Abrir partido: {label} page.mouse fallo ({exc!s})."})
            return False

    async def _mouse_click_row_action_zone(self, page: Page, row: Locator, *, label: str) -> bool:
        """Clic en zona derecha de la fila (donde suele estar el CTA en layout ancho)."""
        try:
            box = await row.first.bounding_box()
            if box is None:
                return False
            x = box["x"] + box["width"] * 0.92
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y)
            await asyncio.sleep(0.05)
            await page.mouse.click(x, y)
            self._emit("log", {"message": f"Abrir partido: {label} (page.mouse zona derecha ~{x:.0f},{y:.0f})"})
            return True
        except Exception as exc:
            self._emit("log", {"message": f"Abrir partido: {label} page.mouse fallo ({exc!s})."})
            return False

    async def close_headless_for_external_chrome(self) -> None:
        """
        Cierra el navegador de Playwright antes de abrir Chrome visible (Dashboard o dialogo).
        Persiste storage_state en session.json para reabrir coherente tras «Continuar hunter».
        """
        ctx = self._active_context
        br = self._active_browser
        if br is None and ctx is None:
            self._emit(
                "log",
                {"message": "close_headless: no hay navegador Playwright activo (quizas ya cerrado)."},
            )
            return
        if ctx is not None:
            try:
                await ctx.storage_state(path=str(self.session_path))
            except Exception as exc:  # noqa: BLE001
                self._emit("log", {"message": f"close_headless: no se pudo guardar session.json ({exc})."})
        if br is not None:
            try:
                await br.close()
            except Exception as exc:  # noqa: BLE001
                self._emit("log", {"message": f"close_headless: aviso al cerrar navegador ({exc})."})
        self._sync_playwright_session_refs(None, None, None)
        self._emit(
            "log",
            {
                "message": (
                    "Playwright (hunter) cerrado antes de Chrome visible: evita sesion paralela y falsos positivos "
                    "FIFA. Pulse «Continuar hunter» para reabrir Playwright con session.json y seguir."
                ),
            },
        )

    async def _reopen_playwright_if_needed(self, p: Playwright, *, resume_url: str) -> tuple[Browser, BrowserContext, Page]:
        """Si el navegador Playwright se cerro (validacion Chrome), recrea contexto desde session.json y vuelve a resume_url."""
        br = self._active_browser
        ctx = self._active_context
        pg = self._active_page
        if br is not None and ctx is not None and pg is not None:
            try:
                if br.is_connected():
                    _ = pg.url
                    return br, ctx, pg
            except Exception:
                pass
        if self._hunter_attach_chrome_cdp():
            br2 = await self._connect_hunter_over_cdp_browser(p)
            ctx2, pg2 = self._attach_pick_existing_fifa_page(br2)
            await self._apply_chromium_stealth_to_page(pg2)
            await self._apply_proxy_bandwidth_routes_if_enabled(pg2)
            await pg2.goto(resume_url, wait_until="domcontentloaded", timeout=90_000)
            self._sync_playwright_session_refs(br2, ctx2, pg2)
            self._emit(
                "log",
                {
                    "message": (
                        f"Playwright reanudado (Chrome CDP attach); URL={resume_url[:120]}"
                        f"{'...' if len(resume_url) > 120 else ''}"
                    ),
                },
            )
            return br2, ctx2, pg2

        headless = self._playwright_headless_launch_arg()
        br2 = await self._launch_playwright_browser(p)
        ctx_kw2: dict[str, Any] = {
            "storage_state": str(self.session_path),
            "locale": "es-MX",
            "ignore_https_errors": self._playwright_ignore_https_errors(),
        }
        if self._use_camoufox() and self._camoufox_auto_fingerprint_screen():
            pass
        elif self._use_camoufox():
            ctx_kw2["viewport"] = self._playwright_viewport_size()
        else:
            ctx_kw2["viewport"] = self._chromium_stealth_viewport()
            ua_re = self._load_chrome_cdp_saved_user_agent()
            if ua_re:
                ctx_kw2["user_agent"] = ua_re
        ctx_kw2.update(self._chromium_context_proxy_kw())
        ctx2 = await br2.new_context(**ctx_kw2)
        pg2 = await ctx2.new_page()
        await self._apply_chromium_stealth_to_page(pg2)
        await self._apply_proxy_bandwidth_routes_if_enabled(pg2)
        await self._try_neutralize_cdp_before_hunter_page_navigation(p)
        await pg2.goto(resume_url, wait_until="domcontentloaded", timeout=90_000)
        self._sync_playwright_session_refs(br2, ctx2, pg2)
        mode = "headless" if headless else "ventana Playwright visible"
        self._emit(
            "log",
            {
                "message": (
                    f"Playwright reanudado ({mode}) con session.json; URL={resume_url[:120]}"
                    f"{'...' if len(resume_url) > 120 else ''}"
                ),
            },
        )
        return br2, ctx2, pg2

    async def _connect_hunter_over_cdp_browser(self, p: Playwright) -> Browser:
        ep = str(self._hunter_cfg().get("cdp_neutralize_endpoint", "http://127.0.0.1:9222")).strip().rstrip("/") or "http://127.0.0.1:9222"
        raw_to = self._hunter_cfg().get("chrome_cdp_attach_connect_timeout_sec", 20.0)
        try:
            cto = max(3.0, float(raw_to))
        except (TypeError, ValueError):
            cto = 20.0
        try:
            return await asyncio.wait_for(p.chromium.connect_over_cdp(ep), timeout=cto)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"No se pudo conectar Playwright a Chrome CDP en {ep!r} (timeout {cto:.0f}s). "
                "Pulse «Iniciar Chrome (CDP 9222)» y deje la tienda FIFA abierta en una pestaña."
            ) from exc

    def _attach_pick_existing_fifa_page(self, browser: Browser) -> tuple[BrowserContext, Page]:
        """
        Reutiliza el BrowserContext existente de CDP (no new_context) y elige una Page con FIFA.
        Prioridad: URL que indique flujo de tienda listo (misma heuristica que captura CDP).
        """
        pairs: list[tuple[BrowserContext, Page]] = []
        for ctx in list(browser.contexts):
            for pg in list(ctx.pages):
                pairs.append((ctx, pg))
        if not pairs:
            raise RuntimeError(
                "Chrome CDP conectado pero sin pestañas visibles. Abra la tienda FIFA en Chrome CDP."
            )
        best: tuple[BrowserContext, Page] | None = None
        for ctx, pg in pairs:
            try:
                url = pg.url or ""
            except Exception:
                continue
            if _url_shows_logged_in_fifa_shop(url):
                return ctx, pg
            if FIFA_HOST in url.lower() and best is None:
                best = (ctx, pg)
        if best is not None:
            return best
        raise RuntimeError(
            "No hay pestaña con tickets.fifa.com en Chrome CDP. Abra la tienda FIFA (logueado) y relance el hunter."
        )

    def _hunter_cfg(self) -> dict[str, Any]:
        return self.config.get("hunter") or {}

    def _criteria(self) -> dict[str, Any]:
        return self.config.get("search_criteria", {}) or {}

    def _proxy_route_blocked_url_substrings(self) -> tuple[str, ...]:
        """Subcadenas de URL (minusculas) para abort tras match; lista en hunter.proxy_route_blocked_url_substrings."""
        raw = self._hunter_cfg().get("proxy_route_blocked_url_substrings")
        if isinstance(raw, list) and len(raw) > 0:
            return tuple(str(x).strip().lower() for x in raw if str(x).strip())
        return tuple(x.lower() for x in _DEFAULT_PROXY_ROUTE_BLOCKED_URL_SUBSTRINGS)

    async def _apply_proxy_bandwidth_routes_if_enabled(self, page: Page) -> None:
        """
        Ahorro de ancho de banda en proxy: aborta resource_type media y dominios de analitica/marketing comunes.
        No toca stylesheet, font, image, script, xhr/fetch (DataDome/FIFA siguen recibiendo lo necesario para pintar).
        Activar con hunter.proxy_route_block_heavy_requests.
        """
        if not _coerce_bool(self._hunter_cfg().get("proxy_route_block_heavy_requests"), default=False):
            return
        frags = self._proxy_route_blocked_url_substrings()

        async def _intercept_heavy_requests(route: Route) -> None:
            try:
                req = route.request
                rt = (req.resource_type or "").lower()
                if rt == "media":
                    await route.abort()
                    return
                url_l = (req.url or "").lower()
                for frag in frags:
                    if frag in url_l:
                        await route.abort()
                        return
                await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _intercept_heavy_requests)
        self._emit(
            "log",
            {
                "message": (
                    "Red (proxy): page.route('**/*') — aborta media + "
                    f"{len(frags)} subcadenas de tracking; no bloquea CSS/fuentes/imagenes/scripts/XHR."
                ),
            },
        )

    def _post_navigation_blind_sec_range(self) -> tuple[float, float]:
        """Pausa ciega post-carga (listado, partido, etc.): editable en hunter.post_navigation_blind_sec_{min,max}."""
        h = self._hunter_cfg()
        try:
            lo = float(h.get("post_navigation_blind_sec_min", 4.5))
        except (TypeError, ValueError):
            lo = 4.5
        try:
            hi = float(h.get("post_navigation_blind_sec_max", 7.5))
        except (TypeError, ValueError):
            hi = 7.5
        if hi < lo:
            lo, hi = hi, lo
        return max(0.05, lo), max(lo, hi)

    def _post_navigation_blind_sec_sample(self) -> float:
        lo, hi = self._post_navigation_blind_sec_range()
        return random.uniform(lo, hi)

    def _post_action_reading_sec_range(self) -> tuple[float, float]:
        """Entre clics en la vista partido (tabs, reservar): hunter.post_action_reading_sec_{min,max}."""
        h = self._hunter_cfg()
        try:
            lo = float(h.get("post_action_reading_sec_min", 1.0))
        except (TypeError, ValueError):
            lo = 1.0
        try:
            hi = float(h.get("post_action_reading_sec_max", 2.5))
        except (TypeError, ValueError):
            hi = 2.5
        if hi < lo:
            lo, hi = hi, lo
        return max(0.05, lo), max(lo, hi)

    async def _micro_reading_sleep_thread(self) -> None:
        """time.sleep en hilo: lectura humana entre acciones en la misma pagina."""
        lo, hi = self._post_action_reading_sec_range()
        await asyncio.to_thread(time.sleep, random.uniform(lo, hi))

    async def _post_navigation_blind_sleep_thread(self, *, log_context: str) -> None:
        """Ceguera inicial: time.sleep en hilo; sin page.locator / wait_for / is_visible en esta corutina."""
        sec = self._post_navigation_blind_sec_sample()
        self._emit(
            "log",
            {
                "message": (
                    f"{log_context}: ceguera inicial {sec:.1f}s "
                    "(time.sleep en hilo; sin consultas DOM en esta corutina)."
                ),
            },
        )
        await asyncio.to_thread(time.sleep, sec)

    async def _pointer_move_human_path_to_locator(
        self, page: Page, locator: Locator, *, timeout_ms: int = 45_000
    ) -> None:
        """Mueve el puntero Playwright hacia el centro del locator en pasos con jitter (sin pulsar)."""
        await locator.wait_for(state="visible", timeout=timeout_ms)
        await locator.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.12, 0.42))
        box = await locator.bounding_box()
        if box is None:
            raise RuntimeError("pointer path: elemento sin bounding_box (¿oculto o fuera de layout?)")
        cx = box["x"] + box["width"] / 2.0
        cy = box["y"] + box["height"] / 2.0
        vp = page.viewport_size
        vw = float(vp["width"]) if vp else 1280.0
        vh = float(vp["height"]) if vp else 720.0
        start_x = max(0.0, min(cx - random.uniform(40.0, 120.0), vw - 1.0))
        start_y = max(0.0, min(cy - random.uniform(40.0, 120.0), vh - 1.0))
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.05, 0.14))
        steps = 10
        for i in range(1, steps + 1):
            t = i / float(steps)
            x = start_x + (cx - start_x) * t + random.uniform(-2.5, 2.5)
            y = start_y + (cy - start_y) * t + random.uniform(-2.5, 2.5)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.008, 0.038))

    async def _warmup_mouse_curved_to_viewport_center(self, page: Page) -> None:
        """Movimiento curvo corto hacia el centro antes de consultas DOM / clic (rompe inactividad)."""
        vp = self._playwright_viewport_size()
        vw = float(vp["width"])
        vh = float(vp["height"])
        last = self._last_bezier_click_viewport
        if last is not None:
            p0 = (max(4.0, min(last[0], vw - 4.0)), max(4.0, min(last[1], vh - 4.0)))
        else:
            p0 = (vw * random.uniform(0.22, 0.78), vh * random.uniform(0.22, 0.78))
        p2 = (vw * random.uniform(0.46, 0.54), vh * random.uniform(0.46, 0.54))
        await self._smooth_mouse_relocate_viewport(page, p0, p2, vw, vh)
        await asyncio.sleep(random.uniform(0.08, 0.22))

    async def human_click(self, page: Page, locator: Locator, *, timeout_ms: int = 45_000) -> None:
        """
        Clic con movimiento de ratón en pasos (DataDome suele penalizar saltos instantáneos al centro).
        Usa asyncio.sleep + jitter (no time.sleep: corutina async).
        """
        await self._pointer_move_human_path_to_locator(page, locator, timeout_ms=timeout_ms)
        await asyncio.sleep(random.uniform(0.06, 0.22))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.035, 0.13))
        await page.mouse.up()
        await asyncio.sleep(random.uniform(0.1, 0.32))

    async def _keyboard_scroll_locator_into_viewport(
        self, page: Page, locator: Locator, *, timeout_ms: int = 45_000
    ) -> bool:
        """Flechas + micro-movimiento de ratón hasta que el elemento quede en viewport. True si hay bbox."""
        await locator.wait_for(state="visible", timeout=timeout_ms)
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        box = await locator.bounding_box()
        intentos_scroll = 0
        vw_vp = float(viewport["width"])
        vh_vp = float(viewport["height"])

        while (
            box
            and (box["y"] + box["height"] > viewport["height"] or box["y"] < 0)
            and intentos_scroll < 15
        ):
            if box["y"] > 0:
                for _ in range(random.randint(2, 4)):
                    await page.keyboard.press("ArrowDown")
                    await asyncio.sleep(random.randint(50, 100) / 1000.0)
            else:
                for _ in range(random.randint(2, 4)):
                    await page.keyboard.press("ArrowUp")
                    await asyncio.sleep(random.randint(50, 100) / 1000.0)

            await asyncio.sleep(random.uniform(0.3, 0.6))
            await page.mouse.move(
                float(random.randint(int(vw_vp * 0.2), int(vw_vp * 0.8))),
                float(random.randint(int(vh_vp * 0.2), int(vh_vp * 0.8))),
            )

            box = await locator.bounding_box()
            intentos_scroll += 1

        return box is not None

    async def _probe_open_match_accessibility_click(self, page: Page, locator: Locator) -> None:
        """
        Apertura partido: scroll teclado → focus + Enter; fallback hover + down/up sin Bézier.
        """
        if not await self._keyboard_scroll_locator_into_viewport(page, locator):
            raise RuntimeError("Probe partido: fila sin bounding_box tras scroll por teclado.")

        try:
            await locator.focus()
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await page.keyboard.press("Enter")
            await asyncio.sleep(random.randint(120, 250) / 1000.0)
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as exc:  # noqa: BLE001
            msg = "El elemento no soporta Enter, usando Hover estático..."
            print(msg, flush=True)
            self._emit("log", {"message": f"Probe partido: {msg} ({exc!s})"})
            await locator.hover(timeout=5_000)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.08, 0.18))
            await page.mouse.up()

    async def bezier_human_click(self, page: Page, locator: Locator, *, timeout_ms: int = 45_000) -> None:
        if not await self._keyboard_scroll_locator_into_viewport(page, locator, timeout_ms=timeout_ms):
            return

        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        box = await locator.bounding_box()
        if not box:
            return

        vw = float(viewport["width"])
        vh = float(viewport["height"])
        margin = 8.0
        target_x = box["x"] + (box["width"] * random.uniform(0.2, 0.8))
        target_y = box["y"] + (box["height"] * random.uniform(0.2, 0.8))
        tx = max(margin, min(target_x, vw - margin))
        ty = max(margin, min(target_y, vh - margin))
        sx = random.uniform(margin, max(margin + 1.0, vw - margin))
        sy = random.uniform(margin, max(margin + 1.0, vh - margin))

        def _quad_bezier(
            p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], t: float
        ) -> tuple[float, float]:
            u = 1.0 - t
            x = u * u * p0[0] + 2.0 * u * t * p1[0] + t * t * p2[0]
            y = u * u * p0[1] + 2.0 * u * t * p1[1] + t * t * p2[1]
            return x, y

        mx = (sx + tx) * 0.5
        my = (sy + ty) * 0.5
        dx = tx - sx
        dy = ty - sy
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            length = 1.0
        px = -dy / length
        py = dx / length
        bend = random.uniform(36.0, 160.0) * random.choice((-1.0, 1.0))
        cx = mx + px * bend + random.uniform(-18.0, 18.0)
        cy = my + py * bend + random.uniform(-18.0, 18.0)
        p0 = (sx, sy)
        p1 = (cx, cy)
        p2 = (tx, ty)

        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.04, 0.11))
        n_steps = random.randint(15, 25)
        brake_start = max(0, n_steps - 3)
        for i in range(n_steps + 1):
            t = i / float(n_steps)
            x, y = _quad_bezier(p0, p1, p2, t)
            x = max(0.0, min(x, vw - 1.0))
            y = max(0.0, min(y, vh - 1.0))
            await page.mouse.move(x, y)
            if i >= brake_start:
                await asyncio.sleep(random.uniform(0.018, 0.048))
            else:
                await asyncio.sleep(random.uniform(0.01, 0.03))

        # 4. Clic final
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.up()
        self._last_bezier_click_viewport = (tx, ty)

    async def _probe_open_match_container_click(self, page: Page, row_loc: Locator) -> None:
        """Apertura partido: scroll teclado + focus/Enter (accesibilidad); sin curva Bézier ni .click() nativo."""
        await self._probe_open_match_accessibility_click(page, row_loc)
        self._emit(
            "log",
            {"message": "Probe: apertura partido con scroll teclado + focus/Enter (fallback hover+down/up)."},
        )

    async def _smooth_mouse_relocate_viewport(
        self, page: Page, p0: tuple[float, float], p2: tuple[float, float], vw: float, vh: float
    ) -> None:
        """Solo page.mouse.move en pasos suaves con arco sinusoidal (evita linea recta); sin .click()."""
        sx, sy = float(p0[0]), float(p0[1])
        tx, ty = float(p2[0]), float(p2[1])
        sx = max(0.0, min(sx, vw - 1.0))
        sy = max(0.0, min(sy, vh - 1.0))
        tx = max(0.0, min(tx, vw - 1.0))
        ty = max(0.0, min(ty, vh - 1.0))
        dx = tx - sx
        dy = ty - sy
        length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        px = -dy / length
        py = dx / length
        amp = random.uniform(16.0, 50.0) * random.choice((-1.0, 1.0))
        n = random.randint(10, 18)
        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.02, 0.06))
        for i in range(1, n + 1):
            t = i / float(n)
            bx = sx + dx * t
            by = sy + dy * t
            arc = math.sin(math.pi * t) * amp * random.uniform(0.9, 1.1)
            x = bx + px * arc + random.uniform(-2.5, 2.5)
            y = by + py * arc + random.uniform(-2.5, 2.5)
            x = max(0.0, min(x, vw - 1.0))
            y = max(0.0, min(y, vh - 1.0))
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.006, 0.02))
        self._last_bezier_click_viewport = (tx, ty)

    async def _probe_blind_delay_and_mouse_wake_after_match_open(self, page: Page) -> None:
        """
        Tras abrir partido: opcional compuerta consola (hunter.team_filter_probe_post_match_console_gate)
        antes de la ceguera larga; luego post_navigation_blind_*; reubicacion del puntero (pasos suaves).
        """
        if _coerce_bool(self._hunter_cfg().get("team_filter_probe_post_match_console_gate"), default=False):
            self._emit(
                "log",
                {
                    "message": (
                        "Probe post-clic partido: compuerta consola activa — antes de la pausa larga (ceguera)."
                    ),
                },
            )
            gate_prompt = (
                "[Probe pre-ceguera] Tras abrir el partido; antes de la pausa larga. "
                "Observe el navegador; pulse ENTER para continuar (ceguera + reubicación ratón + Fase 2)..."
            )
            try:
                await asyncio.to_thread(input, gate_prompt)
            except EOFError:
                pass
        await self._post_navigation_blind_sleep_thread(log_context="Probe post-clic partido")
        vp = self._playwright_viewport_size()
        vw = float(vp["width"])
        vh = float(vp["height"])
        last = self._last_bezier_click_viewport
        if last is None:
            p0 = (vw * 0.5, vh * 0.5)
        else:
            p0 = (max(4.0, min(last[0], vw - 4.0)), max(4.0, min(last[1], vh - 4.0)))
        p2 = (vw * random.uniform(0.40, 0.60), vh * random.uniform(0.36, 0.64))
        self._emit(
            "log",
            {"message": "Probe: reubicacion visual del puntero (pasos suaves hacia zona neutral)."},
        )
        await self._smooth_mouse_relocate_viewport(page, p0, p2, vw, vh)
        await asyncio.sleep(random.uniform(0.15, 0.4))

    async def _find_select_team_locator(self, page: Page) -> Locator | None:
        """select#team en página principal o en iframes."""
        roots: list[SeatUiRoot] = [page]
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            roots.append(fr)
        seen: set[int] = set()
        for root in roots:
            rid = id(root)
            if rid in seen:
                continue
            seen.add(rid)
            loc = root.locator("#team")
            try:
                if await loc.count() == 0:
                    continue
                first = loc.first
                if await first.is_visible():
                    return first
            except Exception:
                continue
        return None

    async def _close_team_select_after_option(self, page: Page, sel: Locator) -> None:
        """
        Quita foco del select#team sin Escape ni Tab.

        Escape (sobre todo dos veces) y Tab pueden propagarse en la SPA de FIFA y a veces
        limpian el filtro de equipo o mueven el foco a otro control: la lista vuelve a mostrar
        todos los partidos de forma intermitente.
        """
        _ = page
        try:
            await sel.evaluate("e => { try { e.blur(); } catch (_) {} }")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.15, 0.4))
        try:
            await sel.evaluate("e => { try { e.blur(); } catch (_) {} }")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.1, 0.28))

    def _page_and_frame_roots(self, page: Page) -> list[SeatUiRoot]:
        """Documento principal + iframes (mismo criterio que #team en listado)."""
        roots: list[SeatUiRoot] = [page]
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            roots.append(fr)
        return roots

    def _team_filter_lang(self) -> str:
        return str(self._hunter_cfg().get("lang", "es") or "es").strip().lower()[:2]

    def _resolve_team_filter_country_name(self, team_id: str) -> str:
        """Nombre en español (u otro idioma futuro) para teclear; nunca el ID numérico."""
        name = resolve_team_country_name(team_id, lang=self._team_filter_lang())
        if not name:
            raise RuntimeError(
                f"Filtro equipo: ID {team_id!r} no está en TEAM_MAPPING (lang={self._team_filter_lang()!r}). "
                "Añádalo en core/team_mapping.py."
            )
        return name

    @staticmethod
    def _native_select_keyboard_label(country_name: str) -> str:
        """Texto para type-ahead del <select> nativo (sin acentos; el SO no ve el DOM de opciones)."""
        folded = unicodedata.normalize("NFKD", country_name)
        return "".join(ch for ch in folded if not unicodedata.combining(ch))

    async def _human_apply_team_country_filter(self, page: Page, sel: Locator, team_id: str) -> None:
        """
        Filtro país/equipo en <select id="team"> nativo: foco Bézier → type-ahead por teclado → Enter.
        Sin select_option, fill, click nativo en opciones ni búsqueda en DOM (menú del SO).
        """
        _ = page
        nombre_equipo = self._resolve_team_filter_country_name(team_id)
        teclas = self._native_select_keyboard_label(nombre_equipo)

        await sel.wait_for(state="visible", timeout=5_000)
        await self.bezier_human_click(page, sel)
        await asyncio.sleep(random.uniform(0.3, 0.6))

        await page.keyboard.type(teclas, delay=random.randint(150, 300))
        await asyncio.sleep(random.uniform(0.4, 0.8))

        await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        self._emit(
            "log",
            {
                "message": (
                    f"Filtro equipo (select nativo): bezier #team + keyboard.type({teclas!r}) "
                    f"+ Enter + pausa lectura lista [{nombre_equipo!r} id={team_id!r}]."
                ),
            },
        )

    async def _gather_aria_and_teams_tokens_for_row(self, row: Locator) -> str:
        """Texto agregado para regex: aria-labelledby en fila y descendientes + ids teams_M*."""
        parts: list[str] = []
        try:
            a = await row.get_attribute("aria-labelledby")
            if a:
                parts.append(a)
        except Exception:
            pass
        nested = row.locator(
            "[aria-labelledby*='event'], [aria-labelledby*='Event'], "
            "[aria-labelledby*='teams'], [id^=\"teams_M\"]"
        )
        try:
            n = await nested.count()
        except Exception:
            n = 0
        for j in range(min(n, 28)):
            try:
                el = nested.nth(j)
                a2 = await el.get_attribute("aria-labelledby") or ""
                if a2:
                    parts.append(a2)
                tid = (await el.get_attribute("id") or "").strip()
                if tid and "teams_m" in tid.lower():
                    parts.append(tid)
            except Exception:
                continue
        return " ".join(parts)

    async def _resolve_teams_paragraph_id_for_row(self, row: Locator) -> tuple[str | None, str | None]:
        """
        Asocia la fila del partido (ya filtrada por pais en data-host/opposing) con el id del <p> encabezado.

        Orden: (1) primer p[id^=teams_M] dentro del li; (2) aria con event-code-MNN → teams_MNN;
        (3) token teams_MNN en aria/ids; (4) event-code-<solo digitos> → teams_M{digitos} si existe el p.
        """
        try:
            cands = row.locator('p[id^="teams_M"]')
            nc = await cands.count()
        except Exception:
            nc = 0
        for j in range(min(nc, 15)):
            try:
                pid = (await cands.nth(j).get_attribute("id") or "").strip()
            except Exception:
                continue
            if re.fullmatch(r"teams_M\d+", pid, flags=re.I):
                return pid, "dom_p_in_row"

        hay = await self._gather_aria_and_teams_tokens_for_row(row)
        if not hay.strip():
            return None, None

        m = _RE_ARIA_EVENT_CODE_M.search(hay)
        if m:
            cand = f"teams_M{m.group(1)}"
            loc = row.locator(f"p#{cand}")
            if await loc.count() == 0:
                loc = row.locator(f'p[id="{cand}"]')
            if await loc.count() > 0:
                return cand, "aria_event_code_M"

        mt = _RE_ARIA_TEAMS_M_TOKEN.search(hay)
        if mt:
            cand_t = mt.group(1)
            if re.fullmatch(r"teams_M\d+", cand_t, flags=re.I):
                loc2 = row.locator(f"p#{cand_t}")
                if await loc2.count() == 0:
                    loc2 = row.locator(f'p[id="{cand_t}"]')
                if await loc2.count() > 0:
                    return cand_t, "aria_teams_m_token"

        if not _RE_ARIA_EVENT_CODE_M.search(hay):
            m2 = _RE_ARIA_EVENT_CODE_NUM.search(hay)
            if m2:
                cand2 = f"teams_M{m2.group(1)}"
                loc3 = row.locator(f"p#{cand2}")
                if await loc3.count() == 0:
                    loc3 = row.locator(f'p[id="{cand2}"]')
                if await loc3.count() > 0:
                    return cand2, "aria_event_code_digits"

        return None, None

    async def _resolve_teams_paragraph_id_row_teams_p_dom_only(self, row: Locator) -> tuple[str | None, str | None]:
        """Solo p[id^=teams_M] dentro del li (sin escaneo aria/event-code; listado ya materializado)."""
        try:
            cands = row.locator('p[id^="teams_M"]')
            nc = await cands.count()
        except Exception:
            nc = 0
        for j in range(min(nc, 15)):
            try:
                pid = (await cands.nth(j).get_attribute("id") or "").strip()
            except Exception:
                continue
            if re.fullmatch(r"teams_M\d+", pid, flags=re.I):
                return pid, "dom_p_in_row"
        return None, None

    async def _find_li_and_teams_p_for_probe(self, page: Page) -> tuple[Locator, Locator, dict[str, Any]] | None:
        """
        Primera fila del listado donde data-host/opposing coincide con target_teams y hay p#teams_MNN
        (solo DOM dentro del li; sin busqueda por aria-labelledby / event-code).
        Confia en el filtro #team previo: recorre todas las filas visibles, sin tope artificial;
        atributos de equipo antes de scroll (menos señal ante DataDome).
        """
        target_ids = self._target_team_ids()
        if not target_ids:
            return None
        rows = await self._match_rows_locator(page)
        try:
            n = await rows.count()
        except Exception:
            n = 0
        if n == 0:
            return None
        for priority_tid in target_ids:
            tid = str(priority_tid).strip()
            for i in range(n):
                if self._stop.is_set():
                    return None
                row = rows.nth(i)
                try:
                    host = await row.get_attribute("data-host-team-id") or await row.get_attribute(
                        "data-home-team-id"
                    )
                    guest = await row.get_attribute("data-opposing-team-id") or await row.get_attribute(
                        "data-away-team-id"
                    )
                except Exception:
                    continue
                ids = {str(x).strip() for x in (host, guest) if x is not None and str(x).strip()}
                if tid not in ids:
                    continue
                try:
                    cls = await row.get_attribute("class") or ""
                except Exception:
                    cls = ""
                if self._row_is_sold_out(cls):
                    continue
                try:
                    await row.scroll_into_view_if_needed(timeout=2_500)
                except Exception:
                    pass
                if await self._row_has_hospitality_cta(row):
                    continue
                teams_pid, resolve_src = await self._resolve_teams_paragraph_id_row_teams_p_dom_only(row)
                if not teams_pid:
                    continue
                p_loc = row.locator(f"p#{teams_pid}")
                if await p_loc.count() == 0:
                    p_loc = row.locator(f'p[id="{teams_pid}"]')
                if await p_loc.count() == 0:
                    continue
                perf_id = await row.get_attribute("id")
                if not perf_id:
                    inner = row.locator("[id]").first
                    if await inner.count() > 0:
                        perf_id = await inner.get_attribute("id")
                aria_l = await row.get_attribute("aria-labelledby")
                mnn_disp = re.sub(r"^teams_M", "", teams_pid, flags=re.I)
                meta: dict[str, Any] = {
                    "target_team_id": priority_tid,
                    "host_team_id": host,
                    "opposing_team_id": guest,
                    "performance_id": perf_id,
                    "aria_labelledby": aria_l,
                    "event_code_mnn": mnn_disp,
                    "teams_p_id": teams_pid,
                    "teams_p_resolve": resolve_src,
                }
                return row, p_loc.first, meta
        return None

    @staticmethod
    def _normalized_team_filter_probe_teams_p_id(raw: Any) -> str:
        """Valor de config: `teams_M3`, `#teams_M3` o `p#teams_M3` → id sin prefijo de selector."""
        if raw is None:
            return ""
        s = str(raw).strip().strip('"').strip("'")
        if not s:
            return ""
        if s.startswith("#"):
            s = s[1:]
        if s.lower().startswith("p#"):
            s = s[2:]
        return s.strip()

    async def _find_fixed_p_teams_in_roots(self, page: Page, elem_id: str) -> Locator | None:
        """`p#elem_id` en documento principal o en cualquier frame (listado FIFA en iframe)."""
        roots: list[SeatUiRoot] = [page]
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            roots.append(fr)
        seen: set[int] = set()
        for root in roots:
            rid = id(root)
            if rid in seen:
                continue
            seen.add(rid)
            loc = root.locator(f"p#{elem_id}")
            if await loc.count() == 0:
                loc = root.locator(f'p[id="{elem_id}"]')
            if await loc.count() == 0:
                continue
            return loc.first
        return None

    async def _run_team_filter_probe_only(self, page: Page, list_url: str) -> None:
        _ = list_url
        sel = await self._find_select_team_locator(page)
        if sel is not None:
            self._emit("log", {"message": "Listado: select#team encontrado (visible); se continua sin confirmacion previa."})
        else:
            self._emit(
                "log",
                {"message": "Listado: select#team no encontrado o no visible; no se puede seleccionar equipo."},
            )
            return

        teams = self._target_team_ids()
        if not teams:
            self._emit("log", {"message": "AVISO: search_criteria.target_teams vacio; no se aplica filtro de equipo."})
            return
        tid = str(teams[0]).strip()
        try:
            await self._human_apply_team_country_filter(page, sel, tid)
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {"message": f"Fallo filtro humanizado en #team: {exc!s}"})
            return

        await self._close_team_select_after_option(page, sel)
        self._emit(
            "log",
            {"message": "select#team: cierre suave (solo blur; sin Escape/Tab) para no resetear el filtro en la SPA FIFA."},
        )

        await asyncio.sleep(random.uniform(0.6, 1.2))
        try:
            await self._wait_for_match_list_populated(page)
        except Exception:
            self._emit("log", {"message": "Lista partidos: aviso — grilla aun vacia o lenta tras filtrar por equipo."})

        fixed = self._normalized_team_filter_probe_teams_p_id(
            self._hunter_cfg().get("team_filter_probe_teams_paragraph_id")
        )
        if fixed:
            msg_fix = (
                f'Probe fijo: buscando <p id="{fixed}"> en pagina e iframes '
                "(sin escaneo dinamico de filas)."
            )
            print(msg_fix, file=sys.stderr, flush=True)
            self._emit("log", {"message": msg_fix})
            p_teams = await self._find_fixed_p_teams_in_roots(page, fixed)
            if p_teams is None:
                self._emit("log", {"message": f'Probe fijo: NO encontrado en DOM p#{fixed} (revisar id en inspector).'})
                return
            try:
                vis = await p_teams.is_visible()
            except Exception:
                vis = False
            self._emit("log", {"message": f"Probe fijo: p#{fixed} encontrado; visible_en_viewport={vis}"})
            perf_fixed = await self._probe_resolve_performance_id_from_p_teams(p_teams)
            if not perf_fixed:
                self._emit(
                    "log",
                    {"message": "Probe fijo: no se obtuvo performance_id del li padre del p; abortado."},
                )
                return
            row_li = p_teams.locator("xpath=ancestor::li[1]").first
            try:
                row_ok = await row_li.count() > 0
            except Exception:
                row_ok = False
            if not row_ok:
                self._emit("log", {"message": "Probe fijo: no hay li ancestro del p (contenedor partido); abortado."})
                return
            await self._probe_open_match_container_click(page, row_li)
            await self._probe_post_teams_p_click_flow(page, {"performance_id": perf_fixed})
            return

        self._emit(
            "log",
            {
                "message": (
                    "Probe lista: primera fila con target_teams y p[id^=teams_M] "
                    "(un solo barrido DOM; sin espera aria ni polling prolongado)."
                ),
            },
        )
        picked = await self._find_li_and_teams_p_for_probe(page)

        if picked is None:
            self._emit(
                "log",
                {
                    "message": (
                        "Partido: NO localizado en un barrido. Revise target_teams, filas agotadas, "
                        f"o defina hunter.team_filter_probe_teams_paragraph_id. target_teams={self._target_team_ids()!r}."
                    ),
                },
            )
            return

        _row, p_teams, meta = picked
        mnn = meta.get("event_code_mnn")
        pid = meta.get("teams_p_id")
        aria = meta.get("aria_labelledby")
        aria_short = (aria or "")[:220] + ("..." if aria and len(str(aria)) > 220 else "")
        self._emit(
            "log",
            {
                "message": (
                    f"Partido identificado: li performance_id={meta.get('performance_id')!r} "
                    f"local={meta.get('host_team_id')!r} visita={meta.get('opposing_team_id')!r} "
                    f"equipo_objetivo={meta.get('target_team_id')!r}; p#{pid} "
                    f"(resuelto_via={meta.get('teams_p_resolve')!r}; sufijo_MNN={mnn!r}); "
                    f"aria-labelledby fragmento: {aria_short!r}"
                ),
            },
        )

        perf_go = str(meta.get("performance_id") or "").strip()
        if not perf_go:
            self._emit("log", {"message": "Probe: performance_id vacio en meta; no clic en contenedor."})
            return
        await self._probe_open_match_container_click(page, _row)
        await self._probe_post_teams_p_click_flow(page, meta)

    async def _probe_resolve_performance_id_from_p_teams(self, p_teams: Locator) -> str | None:
        """Desde p#teams_M* sube al li.performance y lee id (performance_id)."""
        try:
            li_up = p_teams.locator("xpath=ancestor::li[1]")
            if await li_up.count() == 0:
                return None
            pid = (await li_up.first.get_attribute("id") or "").strip()
            return pid or None
        except Exception:
            return None

    async def _probe_wait_dom_and_pause(self, page: Page, *, step_label: str) -> None:
        """Solo pausa configurable: sin wait_for_load_state (DataDome; la pagina asienta sola)."""
        _ = page
        raw = self._hunter_cfg().get("team_filter_probe_step_pause_sec", 10)
        try:
            pause_sec = max(2.0, float(raw))
        except (TypeError, ValueError):
            pause_sec = 10.0
        self._emit(
            "log",
            {"message": f'Probe paso "{step_label}": pausa pasiva {pause_sec:.0f}s (sin wait_for_load_state).'},
        )
        await asyncio.sleep(pause_sec)

    async def _find_first_locator_in_page_and_frames(self, page: Page, selector: str) -> Locator | None:
        """Primer match en documento principal o iframes."""
        roots: list[SeatUiRoot] = [page]
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            roots.append(fr)
        seen: set[int] = set()
        for root in roots:
            rid = id(root)
            if rid in seen:
                continue
            seen.add(rid)
            loc = root.locator(selector)
            try:
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    async def _probe_post_teams_p_click_flow(self, page: Page, meta: dict[str, Any]) -> None:
        # Pausar la ejecución inmediatamente al entrar a la nueva página
        print("🛑 PUNTO SEGURO ALCANZADO: El partido está abriendo. El bot no hará nada más.")
        await asyncio.to_thread(input, "Presiona ENTER para finalizar el script...")

        # Salir de la función prematuramente para no tocar el DOM de la nueva página
        return

    def match_list_url(self) -> str:
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        product_id = str(h.get("product_id", DEFAULT_PRODUCT_ID))
        lang = str(h.get("lang", "es"))
        return f"{host}/secure/selection/event/date/product/{product_id}/lang/{lang}"

    def _canonical_list_path_marker(self) -> str:
        pid = str(self._hunter_cfg().get("product_id", DEFAULT_PRODUCT_ID))
        return f"/product/{pid}/"

    def _tickets_secured_content_url(self) -> str:
        """Landing tienda FIFA (`/secured/content`) alineada con hunter.shop_host."""
        h = str(self._hunter_cfg().get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        return f"{h}/secured/content"

    def _url_host_matches_configured_shop(self, url: str) -> bool:
        try:
            need = (urlparse(self.match_list_url()).netloc or "").lower()
            got = (urlparse(url or "").netloc or "").lower()
            return bool(need and got == need)
        except Exception:
            return False

    def _url_on_secured_storefront_content(self, url: str) -> bool:
        u = (url or "").lower()
        return "/secured/content" in u and self._url_host_matches_configured_shop(url)

    def _url_already_on_match_list(self, url: str) -> bool:
        """True si la URL ya es el listado SSR de partidos (mismo host que shop + ruta date/product)."""
        if not self._url_host_matches_configured_shop(url):
            return False
        u = (url or "").strip()
        if self._canonical_list_path_marker() in u:
            return True
        return bool(_DATE_SELECTION_RE.search(u))

    async def _passive_sync_match_list_dom(self, page: Page, *, timeout_ms: int = 90_000) -> None:
        """Espera pasiva: #team visible y al menos una fila de partido (main + iframes), sin goto."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(45_000, timeout_ms))
        except Exception:
            pass
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            team = await self._find_select_team_locator(page)
            rows = await self._match_rows_locator(page)
            t_ok = False
            if team is not None:
                try:
                    t_ok = await team.is_visible()
                except Exception:
                    t_ok = False
            n = 0
            try:
                n = await rows.count()
            except Exception:
                n = 0
            if t_ok and n > 0:
                try:
                    await rows.first.wait_for(state="visible", timeout=8_000)
                except Exception:
                    pass
                return
            await asyncio.sleep(0.35)
        raise TimeoutError("Lista partidos: timeout pasivo (#team visible y filas performance).")

    async def _organic_page_refresh_f5(self, page: Page) -> None:
        """Recarga tipo usuario (F5); evita page.reload (menos brusco ante DataDome/CDP)."""
        await self._jitter()
        try:
            await page.keyboard.press("F5")
        except Exception:
            try:
                await page.keyboard.press("BrowserRefresh")
            except Exception:
                pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=90_000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.35, 0.95))

    async def _recover_match_list_after_seat_flow_error(self, page: Page, list_url: str) -> None:
        """Vuelve al listado sin reload(); F5 si ya estamos en listado, go_back y si falla goto como ultimo recurso."""
        await self._jitter()
        if self._url_already_on_match_list(page.url):
            self._emit("log", {"message": "Recuperacion: ya en listado; F5 organico + sync pasivo."})
            await self._organic_page_refresh_f5(page)
            await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
            return
        for attempt in range(1, 6):
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:  # noqa: BLE001
                self._emit("log", {"message": f"Recuperacion: go_back intento {attempt}/5 ({exc!s})."})
                break
            await asyncio.sleep(random.uniform(0.35, 0.85))
            if self._url_already_on_match_list(page.url):
                self._emit(
                    "log",
                    {"message": f"Recuperacion: listado alcanzado tras go_back (paso {attempt}); F5 + sync pasivo."},
                )
                await self._organic_page_refresh_f5(page)
                await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
                return
        self._emit(
            "log",
            {
                "message": (
                    "Recuperacion: go_back no dejo el listado; goto listado como ultimo recurso "
                    "(evitar bucle atrapado fuera del listado)."
                ),
            },
        )
        await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
        await self._passive_sync_match_list_dom(page, timeout_ms=90_000)

    def _seat_table_url(self, performance_id: str, table_index: int | None = None) -> str:
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        lang = str(h.get("lang", "es"))
        idx = table_index if table_index is not None else int(h.get("seat_table_index", 1))
        return (
            f"{host}/secure/selection/event/seat/performance/{performance_id}"
            f"/table/{idx}/lang/{lang}"
        )

    def _seat_performance_landing_url(self, performance_id: str) -> str:
        """Vista intermedia de asiento (sin /table) que a veces muestra tabs como #tab-2-link."""
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        lang = str(h.get("lang", "es"))
        return f"{host}/secure/selection/event/seat/performance/{performance_id}/lang/{lang}"

    @staticmethod
    def _seat_table_url_reached(url: str, performance_id: str) -> bool:
        """True si la URL ya es la vista canonica de grilla /performance/<id>/table/..."""
        u = (url or "").strip()
        if not u:
            return False
        pid = str(performance_id).strip()
        ul = u.lower()
        return bool(pid) and pid in u and "/performance/" in ul and "/table/" in ul

    async def _click_match_row_for_seat_entry(self, page: Page, row: Locator, performance_id: str) -> None:
        """
        Clic en «Seleccionar» del partido elegido.

        FIFA suele poner el handler en span.performance-select-btn; el <a href="#"> con
        onclick=\"return false;\" puede ignorar el clic sintético si solo se pulsa el <a>.
        """
        perf = str(performance_id).strip()
        await self._ensure_desktop_viewport_for_match_list(page)
        await row.scroll_into_view_if_needed()

        book = row.locator(f"a#book{perf}")
        span_btn = row.locator("span.performance-select-btn")
        role_btn = row.get_by_role("button", name=re.compile(r"seleccionar", re.I))

        async def _try_click(label: str, loc: Locator) -> bool:
            try:
                if await loc.count() == 0:
                    return False
                el = loc.first
                await el.scroll_into_view_if_needed(timeout=10_000)
                await el.wait_for(state="visible", timeout=15_000)
                await el.click(timeout=45_000)
                self._emit("log", {"message": f"Abrir partido: {label} (clic Playwright)"})
                return True
            except Exception as exc:
                self._emit("log", {"message": f"Abrir partido: {label} clic normal fallo ({exc!s}); probando force."})
                try:
                    if await loc.count() > 0:
                        await loc.first.click(timeout=45_000, force=True)
                        self._emit("log", {"message": f"Abrir partido: {label} (clic FORCE)"})
                        return True
                except Exception as exc2:
                    self._emit("log", {"message": f"Abrir partido: {label} FORCE fallo ({exc2!s})."})
                return False

        async def _dispatch_click(loc: Locator, label: str) -> bool:
            try:
                if await loc.count() == 0:
                    return False
                await loc.first.evaluate(
                    """(el) => {
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    }"""
                )
                self._emit("log", {"message": f"Abrir partido: {label} (dispatchEvent click en DOM)"})
                return True
            except Exception as exc:
                self._emit("log", {"message": f"Abrir partido: {label} dispatchEvent fallo ({exc!s})."})
                return False

        async def _js_wrap_then_anchor() -> bool:
            """Prioriza .click() en el envoltorio (comportamiento cercano al usuario real)."""
            try:
                if await book.count() == 0:
                    return False
                await book.first.evaluate(
                    """(el) => {
                        const wrap = el.closest('.performance-select-btn');
                        if (wrap) {
                            wrap.click();
                        } else {
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        }
                    }"""
                )
                self._emit(
                    "log",
                    {"message": "Abrir partido: JS — wrap.performance-select-btn.click() o dispatch en <a>."},
                )
                return True
            except Exception as exc:
                self._emit("log", {"message": f"Abrir partido: JS wrap/anchor fallo ({exc!s})."})
                return False

        # Esperar a que el CTA exista y no quede aria-disabled=true (a veces FIFA lo habilita tras un tick)
        last_note = ""
        for _ in range(100):
            try:
                if await book.count() == 0 and await span_btn.count() == 0:
                    last_note = "sin a#book ni span en fila"
                    await asyncio.sleep(0.25)
                    continue
                if await book.count() > 0:
                    ad = (await book.first.get_attribute("aria-disabled") or "").strip().lower()
                    if ad in ("true", "1"):
                        last_note = f"a#book{perf} aria-disabled={ad!r}"
                        await asyncio.sleep(0.25)
                        continue
                    if await book.first.is_visible():
                        break
                if await span_btn.count() > 0 and await span_btn.first.is_visible():
                    break
                last_note = "CTA aun no visible"
            except Exception as exc:
                last_note = str(exc)
            await asyncio.sleep(0.25)
        else:
            self._emit(
                "log",
                {"message": f"Abrir partido: tras ~25s el CTA sigue sin estar listo ({last_note}). Se intenta igual."},
            )

        if await _try_click("span.performance-select-btn", span_btn):
            return
        if await _try_click(f"a#book{perf}", book):
            return
        if await _try_click("role=button[name~=Seleccionar]", role_btn):
            return
        if await _dispatch_click(span_btn, "span.performance-select-btn"):
            return
        if await _dispatch_click(book, f"a#book{perf}"):
            return
        if await _js_wrap_then_anchor():
            return

        if await self._mouse_click_viewport_center_of(page, span_btn, label="span.performance-select-btn"):
            return
        if await self._mouse_click_viewport_center_of(page, book, label=f"a#book{perf}"):
            return
        if await self._mouse_click_viewport_center_of(page, role_btn, label="role=button Seleccionar"):
            return
        if await self._mouse_click_row_action_zone(
            page, row, label="fila (zona derecha tipo clic en fila humana)"
        ):
            return

        selectors = (
            f"a#book{perf}",
            "span.performance-select-btn a[id^='book']",
            "a[id^='book']",
            "span.performance-select-btn a[role='button']",
            "a:has-text('Seleccionar')",
            "a[href*='/selection/event/seat']",
            "a[href*='performance']",
            "a[href*='perfId']",
            "a[href*='perf']",
            "a",
        )
        list_roots: list[SeatUiRoot] = [page]
        list_roots.extend(await self._fifa_product_list_frames_from_iframe_src(page))
        list_roots.extend(self._fifa_product_list_frames(page))
        seen_roots: set[int] = set()
        for root in list_roots:
            rid = id(root)
            if rid in seen_roots:
                continue
            seen_roots.add(rid)
            scoped = root.locator(f"a#book{perf}")
            if await scoped.count() > 0:
                if await _try_click(f"global iframe/main a#book{perf}", scoped):
                    return
            for sel in selectors:
                link = root.locator(sel).first
                try:
                    if await link.count() == 0:
                        continue
                    await link.wait_for(state="visible", timeout=4_000)
                    await link.click(timeout=45_000)
                    self._emit("log", {"message": f"Abrir partido: clic global => {sel}"})
                    return
                except Exception:
                    try:
                        if await link.count() > 0:
                            await link.first.click(timeout=45_000, force=True)
                            self._emit("log", {"message": f"Abrir partido: clic FORCE global => {sel}"})
                            return
                    except Exception:
                        continue

        for sel in selectors:
            link = row.locator(sel).first
            try:
                if await link.count() == 0:
                    continue
                await link.wait_for(state="visible", timeout=4_000)
                await link.click(timeout=45_000)
                self._emit("log", {"message": f"Abrir partido: clic en fila => {sel}"})
                return
            except Exception:
                try:
                    if await link.count() > 0:
                        await link.first.click(timeout=45_000, force=True)
                        self._emit("log", {"message": f"Abrir partido: FORCE en fila => {sel}"})
                        return
                except Exception:
                    continue

        self._emit(
            "log",
            {
                "message": (
                    "Abrir partido: fallback a clic en <li> (no hubo CTA Seleccionar accionable en fila/global)."
                ),
            },
        )
        await row.click(timeout=45_000)

    async def _click_first_visible_in_locators(
        self, entries: list[tuple[str, Locator]], timeout: int
    ) -> bool:
        """Prueba cada locator en orden; solo hace clic en candidatos visibles."""
        for label, loc in entries:
            n = await loc.count()
            for i in range(n):
                cand = loc.nth(i)
                try:
                    if not await cand.is_visible():
                        continue
                    await cand.click(timeout=timeout)
                    self._emit("log", {"message": f"COMPRAR BOLETOS: {label} (coincidencias={n}, i={i})."})
                    return True
                except Exception:
                    continue
        return False

    def _href_looks_like_date_selection(self, href: str) -> bool:
        h = href.lower()
        return (
            "selection/event/date" in h
            or "productid=" in h
            or "/product/" in h  # path-style /date/product/<id>/...
        )

    async def _click_comprar_boletos(self, page: Page) -> None:
        timeout = 60_000
        # FIFA mezcla /secure/ y /secured/; el CTA real suele llevar selection/event/date en href.
        # No usar .first sin comprobar visibilidad: el primer match puede ser role=menuitem oculto
        # con href=/secured/content (menú hamburguesa).
        strategies: list[tuple[str, Locator]] = [
            (
                "stx-MainActionArea+date",
                page.locator('a.stx-MainActionArea[href*="selection/event/date"]'),
            ),
            (
                "g-Button-primary+date",
                page.locator('a.g-Button-primary[href*="selection/event/date"]'),
            ),
            (
                "stx-ProductCard+date",
                page.locator('div[class*="stx-ProductCard"] a[href*="selection/event/date"]'),
            ),
            (
                "any-a-selection-date",
                page.locator('a[href*="selection/event/date"]'),
            ),
        ]
        if await self._click_first_visible_in_locators(strategies, timeout):
            return

        link = page.get_by_role("link", name=re.compile(r"comprar\s+boletos", re.I))
        n_link = await link.count()
        for i in range(n_link):
            cand = link.nth(i)
            try:
                if not await cand.is_visible():
                    continue
                href = (await cand.get_attribute("href")) or ""
                if not self._href_looks_like_date_selection(href):
                    continue
                await cand.click(timeout=timeout)
                self._emit("log", {"message": f"COMPRAR BOLETOS: link por rol visible (i={i})."})
                return
            except Exception:
                continue

        text_any = page.locator("a").filter(has_text=re.compile(r"comprar\s+boletos", re.I))
        n_txt = await text_any.count()
        for i in range(n_txt):
            cand = text_any.nth(i)
            try:
                if not await cand.is_visible():
                    continue
                href = (await cand.get_attribute("href")) or ""
                if not self._href_looks_like_date_selection(href):
                    continue
                await cand.click(timeout=timeout)
                self._emit("log", {"message": f"COMPRAR BOLETOS: texto + href seleccion (i={i})."})
                return
            except Exception:
                continue

        list_url = self.match_list_url()
        self._emit(
            "log",
            {
                "message": (
                    "COMPRAR BOLETOS: sin CTA visible hacia fechas; se abre el listado directamente "
                    f"(headless/DOM): {list_url[:100]}..."
                ),
            },
        )
        cur = (page.url or "").strip()
        if self._url_already_on_match_list(cur):
            self._emit(
                "log",
                {"message": "COMPRAR BOLETOS: URL ya es listado de partidos; se omite goto directo."},
            )
            await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
            return
        await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)

    async def _wait_url_matches(
        self, page: Page, pattern: re.Pattern[str], timeout_ms: int, label: str
    ) -> None:
        """
        Espera a que page.url coincida con pattern (polling).
        Evita page.wait_for_url(...): por defecto espera el evento 'load', y muchas SPAs
        (tienda FIFA) no lo disparan de forma fiable pese a tener ya la URL correcta.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if pattern.search(page.url):
                return
            await asyncio.sleep(0.2)
        self._emit("log", {"message": f"URL al timeout ({label}): {page.url}"})
        raise TimeoutError(f"Timeout esperando URL ({label}): {page.url!r}")

    async def _cdp_async_navigate_all_about_blank(self, browser: Browser) -> bool:
        """Todas las pestañas de la instancia CDP a about:blank; espera URLs (paridad con SessionManager)."""
        pages: list[Page] = []
        for ctx in list(browser.contexts):
            pages.extend(list(ctx.pages))
        if not pages:
            try:
                if browser.contexts:
                    pages = [browser.contexts[0].new_page()]
                else:
                    return False
            except Exception:
                return False
        for pg in pages:
            try:
                await pg.goto("about:blank", wait_until="load", timeout=25_000)
            except Exception:
                pass
        deadline = time.monotonic() + 18.0
        urls: list[str] = []
        while time.monotonic() < deadline:
            cur: list[Page] = []
            for ctx in list(browser.contexts):
                cur.extend(list(ctx.pages))
            if not cur:
                await asyncio.sleep(0.2)
                continue
            urls = [(x.url or "").strip().lower() for x in cur]
            if all(u == "about:blank" or u == "about:srcdoc" for u in urls):
                return True
            await asyncio.sleep(0.2)
        self._emit("log", {"message": f"Hunter CDP neutral: timeout about:blank; URLs={urls!r}"})
        return False

    async def _try_neutralize_cdp_before_hunter_page_navigation(self, p: Playwright) -> None:
        """
        Si Chrome CDP sigue en 9222, conecta con este Playwright, lleva todas las pestañas a about:blank
        y desconecta. Así el primer page.goto del hunter no compite con scripts FIFA en Chrome CDP.
        Omitido si attach_hunter_to_chrome_cdp (no vaciar la pestaña que usa el hunter).
        """
        if self._hunter_attach_chrome_cdp():
            return
        if not _coerce_bool(self._hunter_cfg().get("cdp_neutralize_before_hunter_goto", True), default=True):
            return
        ep = str(self._hunter_cfg().get("cdp_neutralize_endpoint", "http://127.0.0.1:9222")).strip().rstrip("/") or "http://127.0.0.1:9222"
        raw_to = self._hunter_cfg().get("cdp_neutralize_connect_timeout_sec", 4.0)
        try:
            cto = max(0.5, float(raw_to))
        except (TypeError, ValueError):
            cto = 4.0
        try:
            b = await asyncio.wait_for(p.chromium.connect_over_cdp(ep), timeout=cto)
        except Exception:
            self._emit(
                "log",
                {
                    "message": (
                        f"Hunter: Chrome CDP no conectado en {ep!r} ({cto:.1f}s); "
                        "se omite neutralizar about:blank (normal si Chrome CDP esta cerrado)."
                    ),
                },
            )
            return
        try:
            ok = await self._cdp_async_navigate_all_about_blank(b)
            self._emit(
                "log",
                {
                    "message": (
                        "Hunter: pestañas Chrome CDP en about:blank antes del goto Playwright "
                        f"(confirmado={ok}); sesion CDP auxiliar desconectada."
                    ),
                },
            )
        finally:
            try:
                await b.close()
            except Exception:
                pass

    async def _enter_match_list_page(self, p: Playwright, page: Page) -> str:
        """
        Devuelve la URL canónica del listado usada para el bucle (match_list_url).
        Por defecto no pasa por /secured/content (open_secured_content_tienda: false).

        Si la pestaña ya está en el listado o en /secured/content de la misma tienda, se omite el
        page.goto inicial (menos señal de recarga dura para DataDome). Siempre se sincroniza el DOM
        pasando por #team visible y filas de partidos.
        """
        await self._try_neutralize_cdp_before_hunter_page_navigation(p)
        list_url = self.match_list_url()
        skip_home = bool(self._hunter_cfg().get("skip_secured_content", False))
        open_tienda = _coerce_bool(self._hunter_cfg().get("open_secured_content_tienda", False))
        cur = (page.url or "").strip()
        tienda_home = self._tickets_secured_content_url()

        if skip_home or not open_tienda:
            if self._url_already_on_match_list(cur):
                self._emit(
                    "log",
                    {
                        "message": (
                            "Listado: la URL actual ya es la de partidos (product/lang o date); "
                            "se omite page.goto inicial (recarga dura / CDP)."
                        ),
                    },
                )
                await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
                await self._post_navigation_blind_sleep_thread(
                    log_context="Listado (ya en SSR; post sync)",
                )
                return list_url
            await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
            self._emit(
                "log",
                {"message": f'Pagina de "Listado de partidos" (goto): {list_url}'},
            )
            await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
            await self._post_navigation_blind_sleep_thread(log_context="Listado (post goto SSR)")
            return list_url

        if self._url_already_on_match_list(cur):
            self._emit(
                "log",
                {
                    "message": (
                        "Listado: URL actual ya es listado de partidos; se omite tienda + goto listado."
                    ),
                },
            )
            await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
            await self._post_navigation_blind_sleep_thread(
                log_context="Listado (ya en SSR; post sync)",
            )
            return list_url

        if self._url_on_secured_storefront_content(cur):
            self._emit(
                "log",
                {"message": "Tienda: ya en /secured/content del shop configurado; se omite goto a landing."},
            )
        else:
            await page.goto(tienda_home, wait_until="domcontentloaded", timeout=90_000)
        await self._click_comprar_boletos(page)
        await self._wait_url_matches(page, _DATE_SELECTION_RE, 90_000, "selection/event/date")
        self._emit("log", {"message": f"Tras COMPRAR BOLETOS: {page.url[:140]}..."})

        if self._canonical_list_path_marker() not in page.url:
            after = (page.url or "").strip()
            if self._url_already_on_match_list(after):
                self._emit(
                    "log",
                    {"message": "Listado: URL ya canónica tras COMPRAR BOLETOS; se omite goto de normalización."},
                )
            else:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
                self._emit("log", {"message": "Normalizado a URL canónica del listado SSR (product/lang)."})

        await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
        await self._post_navigation_blind_sleep_thread(log_context="Listado (entrada tienda + SSR)")
        return list_url

    def _normalized_speed_key(self) -> str:
        raw = self._hunter_cfg().get("speed", "baja")
        if not isinstance(raw, str):
            return "baja"
        key = raw.strip().lower()
        key = _SPEED_ALIASES.get(key, key)
        return key if key in _SPEED_BOUNDS_SEC else "baja"

    def _jitter_bounds_sec(self) -> tuple[float, float]:
        return _SPEED_BOUNDS_SEC[self._normalized_speed_key()]

    def jitter_profile(self) -> dict[str, Any]:
        lo, hi = self._jitter_bounds_sec()
        key = self._normalized_speed_key()
        return {
            "speed": key,
            "min_sec": lo,
            "max_sec": hi,
            "min_ms": int(round(lo * 1000)),
            "max_ms": int(round(hi * 1000)),
        }

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._on_event:
            return
        try:
            result = self._on_event(event_type, payload)
            if inspect.isawaitable(result):
                asyncio.create_task(result)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            _ = exc

    @staticmethod
    def performance_id_from_url(url: str) -> str | None:
        u = url or ""
        m = _PERF_ID_IN_URL_RE.search(u)
        if m:
            return m.group(1)
        mq = _PERF_ID_QUERY_RE.search(u)
        return mq.group(1) if mq else None

    def _emit_captcha_handoff_required(self, page: Page, *, step: str) -> None:
        """UI/CLI: mismo payload para Dashboard Flet y scripts/run_hunter.py."""
        self._emit(
            "captcha_handoff_required",
            {
                "step": step,
                "handoff_url": page.url,
                "performance_id": self.performance_id_from_url(page.url),
                "target_teams": self._target_team_ids(),
                "instructions_es": (
                    "El sitio muestra proteccion DataDome (captcha). El modo headless no puede resolverla.\n\n"
                    "1) Detenga el hunter. Cierre Chrome CDP si seguia abierto en paralelo al headless.\n"
                    "2) Abra la URL en Chrome con la misma sesion que uso para session.json (perfil CDP recomendado).\n"
                    "3) Complete el captcha y los pasos que FIFA pida.\n"
                    "4) En esta app: Capturar session.json otra vez desde el onboarding.\n"
                    "5) Reinicie la caceria cuando quiera."
                ),
            },
        )

    async def _captcha_handoff_continue_same_run(
        self, p: Playwright, handoff_url: str, meta: dict[str, Any]
    ) -> bool:
        """
        Flujo alternativo: abrir navegador visible con la misma sesión para que el usuario
        resuelva captcha, y retomar selección + add-to-cart en esta misma corrida.
        """
        if not bool(self._hunter_cfg().get("enable_captcha_handoff_continue", True)):
            return False

        raw_to = self._hunter_cfg().get("captcha_handoff_wait_sec", 240)
        try:
            wait_sec = max(20.0, float(raw_to))
        except (TypeError, ValueError):
            wait_sec = 240.0

        self._emit(
            "log",
            {
                "message": (
                    "Captcha handoff: se abre navegador visible (idealmente Google Chrome instalado) "
                    "con la misma session.json para resolver DataDome y reintentar add-to-cart en esta corrida."
                ),
            },
        )

        use_chrome = bool(self._hunter_cfg().get("handoff_use_google_chrome", True))
        handoff_args = ["--disable-blink-features=AutomationControlled"]
        if self._playwright_ignore_https_errors():
            handoff_args.extend(["--ignore-certificate-errors", "--test-type"])
        launch_kwargs: dict[str, Any] = {
            "headless": False,
            "args": handoff_args,
            "ignore_default_args": ["--enable-automation"],
        }
        handoff_proxy = resolve_playwright_proxy(self._hunter_cfg())
        if handoff_proxy is not None:
            launch_kwargs["proxy"] = handoff_proxy
            self._emit("log", {"message": f"Captcha handoff: proxy en launch() ({handoff_proxy.get('server')})."})
        if use_chrome:
            launch_kwargs["channel"] = "chrome"

        try:
            headed_browser = await p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            self._emit(
                "log",
                {
                    "message": (
                        f"Handoff: no se pudo abrir con Google Chrome ({exc!s}); "
                        "reintentando con Chromium embebido (puede disparar mas restricciones FIFA)."
                    ),
                },
            )
            fb_args = ["--disable-blink-features=AutomationControlled"]
            if self._playwright_ignore_https_errors():
                fb_args.extend(["--ignore-certificate-errors", "--test-type"])
            fb_kw: dict[str, Any] = {
                "headless": False,
                "args": fb_args,
                "ignore_default_args": ["--enable-automation"],
            }
            if handoff_proxy is not None:
                fb_kw["proxy"] = handoff_proxy
            headed_browser = await p.chromium.launch(**fb_kw)

        ua_h = self._load_chrome_cdp_saved_user_agent()
        headed_ctx_kw: dict[str, Any] = {
            "storage_state": str(self.session_path),
            "viewport": self._chromium_stealth_viewport(),
            "locale": "es-MX",
            "ignore_https_errors": self._playwright_ignore_https_errors(),
        }
        if ua_h:
            headed_ctx_kw["user_agent"] = ua_h
        if handoff_proxy is not None:
            headed_ctx_kw["proxy"] = handoff_proxy
        headed_ctx = await headed_browser.new_context(**headed_ctx_kw)
        headed_page = await headed_ctx.new_page()
        handoff_stealth = Stealth(navigator_user_agent_override=ua_h) if ua_h else Stealth()
        await handoff_stealth.apply_stealth_async(headed_page)
        await self._apply_proxy_bandwidth_routes_if_enabled(headed_page)
        await headed_page.goto(handoff_url, wait_until="domcontentloaded", timeout=90_000)

        if await self._page_fifa_bot_wall(headed_page):
            self._emit(
                "error",
                {
                    "message": (
                        "Handoff visible: FIFA muestra bloqueo / «acceso restringido». Suele deberse a la "
                        "combinacion headless + segunda ventana, ritmo de peticiones o senal de riesgo en la IP. "
                        "Espere 15-45 min sin tocar la tienda, use solo un navegador (Chrome CDP del onboarding), "
                        "limpie o renueve app.biting_lobster_chrome_profile si hace falta, capture session.json "
                        "de nuevo y evite ejecutar hunter hasta tener sesion estable en Chrome visible."
                    ),
                    "recoverable": True,
                    "url": headed_page.url,
                },
            )
            self._emit(
                "log",
                {
                    "message": (
                        "Handoff: no se continuara la automatizacion mientras la pagina muestre restriccion. "
                        "Cierre la ventana del handoff, siga los pasos del mensaje ERROR anterior y vuelva a lanzar."
                    ),
                },
            )
            try:
                await headed_ctx.storage_state(path=str(self.session_path))
            except Exception:
                pass
            await headed_browser.close()
            return False

        self._emit_captcha_handoff_required(headed_page, step="seat_category_table_visible_handoff")
        self._emit(
            "log",
            {
                "message": (
                    f"Handoff visible activo ({int(wait_sec)}s max): resuelve captcha y deja abierta la "
                    f"tabla para continuar. URL={handoff_url[:140]}"
                ),
            },
        )

        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline and not self._stop.is_set():
            rows = await self._locate_category_table_rows(await self._detect_seat_ui_root_once(headed_page))
            try:
                if await rows.count() > 0 and not self._datadome_captcha_iframe_present(headed_page):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.8)

        try:
            pick = await self._pick_category_table_row_and_quantity(headed_page)
            if pick is None:
                self._emit("log", {"message": "Handoff visible: no se pudo seleccionar categoría tras captcha."})
                await headed_ctx.storage_state(path=str(self.session_path))
                await headed_browser.close()
                return False

            await self._jitter()
            await self._humanized_click_book(headed_page)
            await self._jitter()
            await headed_ctx.storage_state(path=str(self.session_path))
            self._emit(
                "ticket_secured",
                {
                    "message": "Boleto Asegurado",
                    "quantity": int(pick["quantity"]),
                    "price_hint": pick["price_hint"],
                    "performance_id": meta.get("performance_id")
                    or self.performance_id_from_url(headed_page.url),
                    "category": pick.get("category"),
                },
            )
            await headed_browser.close()
            return True
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {"message": f"Handoff visible: fallo al retomar flujo de compra ({exc})."})
            try:
                await headed_ctx.storage_state(path=str(self.session_path))
            except Exception:
                pass
            await headed_browser.close()
            return False

    async def _jitter(self) -> None:
        lo, hi = self._jitter_bounds_sec()
        if hi < lo:
            lo, hi = hi, lo
        await asyncio.sleep(random.uniform(lo, hi))

    def _target_team_ids(self) -> list[str]:
        teams = self._criteria().get("target_teams") or []
        return [str(t) for t in teams]

    def _price_within_budget(self, hint: dict[str, Any] | None) -> bool:
        if not hint:
            return True
        max_cents = int(self._criteria().get("max_price_cents", 999999999))
        try:
            conv = CurrencyConverter(self.config)
            cents = conv.to_usd_cents(float(hint["amount"]), str(hint.get("currency", "USD")))
            return cents <= max_cents
        except Exception:
            return True

    def _hint_from_amount_attrs(
        self, raw: str | None, cls: str, context_text: str | None = None
    ) -> dict[str, Any] | None:
        if not raw:
            return None
        currency = "USD"
        from_cls = False
        for code in ("MXN", "USD", "CAD"):
            if f"amount_{code}" in cls:
                currency = code
                from_cls = True
                break
        if not from_cls and context_text:
            cu = context_text.upper()
            if "MXN" in cu:
                currency = "MXN"
            elif "CAD" in cu:
                currency = "CAD"
        try:
            minor = int(str(raw).strip())
        except ValueError:
            return None
        amount_major = minor / 1000.0
        return {"amount": amount_major, "currency": currency}

    async def _price_from_amount_element(
        self, target: Locator, *, context_text: str | None = None
    ) -> dict[str, Any] | None:
        if await target.count() == 0:
            return None
        el = target.first
        raw = await el.get_attribute("data-amount")
        cls = await el.get_attribute("class") or ""
        return self._hint_from_amount_attrs(raw, cls, context_text)

    async def _price_from_amount_span(self, span: Locator) -> dict[str, Any] | None:
        return await self._price_from_amount_element(span)

    @staticmethod
    def _row_is_sold_out(class_attr: str) -> bool:
        """
        FIFA suele usar clases compuestas (p. ej. tokens con guiones) sin la palabra suelta
        'available' tras split por espacios; solo tratamos de excluir filas claramente agotadas.
        """
        if not class_attr:
            return False
        lo = class_attr.lower().replace("_", "-")
        if "sold-out" in lo or "soldout" in lo or "agotado" in lo:
            return True
        if "no-disponible" in lo or "not-available" in lo:
            if "limited" in lo or "limitada" in lo:
                return False
            return True
        return False

    async def _price_from_row(self, row: Locator) -> dict[str, Any] | None:
        try:
            ctx = await row.inner_text()
        except Exception:
            ctx = None
        return await self._price_from_amount_element(
            row.locator("span.amount[data-amount]").first, context_text=ctx
        )

    @staticmethod
    def _category_number_from_label_text(text: str) -> int | None:
        """Número de categoría en leyendas FIFA: 'Categoría 2', 'Zona delantera (categoría 1)', etc."""
        if not text or not str(text).strip():
            return None
        m = re.search(r"categor[ií]a\s*(\d)", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\bcategory\s*(\d)\b", text, re.I)
        if m:
            return int(m.group(1))
        return None

    async def _row_has_hospitality_cta(self, row: Locator) -> bool:
        """
        Opcional: excluir filas cuyo CTA principal sea paquete Hospitality.
        Por defecto OFF: FIFA suele incluir enlaces secundarios con "hospitality" en la URL
        en casi todas las filas y eso descartaba partidos válidos (p. ej. Canadá).
        Activa con hunter.skip_hospitality_filter: false solo si confirmas que el DOM no mete ese ruido.
        """
        if not bool(self._hunter_cfg().get("skip_hospitality_filter", True)):
            href_h = row.locator(
                'a[href*="hospitality"], a[href*="Hospitality"], '
                'a[href*="hospitalidad"], a[href*="HOSPITALITY"]'
            )
            return await href_h.count() > 0
        return False

    def _fifa_product_list_frames(self, page: Page) -> list[Frame]:
        """Listado de fechas/partidos suele ir en iframe; el main puede ser shell vacío."""
        out: list[Frame] = []
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            u = self._frame_url_lower(fr)
            if self._is_datadome_or_captcha_frame_url(u):
                continue
            if "tickets.fifa.com" not in u:
                continue
            if "/selection/event/date" in u or "/product/" in u:
                out.append(fr)
        return out

    async def _fifa_product_list_frames_from_iframe_src(self, page: Page) -> list[Frame]:
        """Emparejar frames cuando Frame.url tarda; usa <iframe src> del shell."""
        picked: list[Frame] = []
        seen: set[int] = set()

        def _add(fr: Frame) -> None:
            rid = id(fr)
            if rid in seen or fr == page.main_frame:
                return
            seen.add(rid)
            picked.append(fr)

        for css in (
            'iframe[src*="selection/event/date"]',
            'iframe[src*="/product/"]',
            'iframe[src*="productId"]',
        ):
            el = page.locator(css).first
            if await el.count() == 0:
                continue
            raw = (await el.get_attribute("src")) or ""
            if not raw.strip():
                continue
            abs_src = urljoin(page.url, raw.strip())
            for fr in page.frames:
                fu = fr.url or ""
                if self._urls_loosely_match_iframe_src(abs_src, fu):
                    _add(fr)
            for _ in range(20):
                if picked:
                    break
                await asyncio.sleep(0.1)
                for fr in page.frames:
                    fu = fr.url or ""
                    if self._urls_loosely_match_iframe_src(abs_src, fu):
                        _add(fr)
        return picked

    async def _match_rows_locator(self, page: Page) -> Locator:
        """Varias variantes de lista SSR; iframes FIFA del listado antes que el documento principal."""
        dom_frames = await self._fifa_product_list_frames_from_iframe_src(page)
        url_frames = self._fifa_product_list_frames(page)
        roots: list[SeatUiRoot] = []
        seen: set[int] = set()
        for r in (*dom_frames, *url_frames, page):
            rid = id(r)
            if rid in seen:
                continue
            seen.add(rid)
            roots.append(r)
        for root in roots:
            for sel in (
                "li[data-host-team-id][data-opposing-team-id]",
                "li.performance",
                "li[class*='performance']",
                "li[class*='Performance']",
                "[data-host-team-id][data-opposing-team-id]",
            ):
                loc = root.locator(sel)
                try:
                    if await loc.count() > 0:
                        return loc
                except Exception:
                    continue
        return page.locator("li[data-host-team-id][data-opposing-team-id]")

    async def _wait_for_match_list_populated(self, page: Page) -> None:
        """Evita wait_for sobre li.performance cuando count=0 (timeout engañoso); sondea main + iframes."""
        raw = self._hunter_cfg().get("match_list_first_row_timeout_sec", 45)
        try:
            timeout_sec = max(8.0, float(raw))
        except (TypeError, ValueError):
            timeout_sec = 45.0
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            loc = await self._match_rows_locator(page)
            try:
                n = await loc.count()
                if n > 0:
                    await loc.first.wait_for(state="attached", timeout=8_000)
                    return
            except Exception:
                pass
            await asyncio.sleep(0.4)
        raise TimeoutError("Lista partidos: sin filas conocidas (main + iframes) tras espera")

    async def _emit_match_list_cart_or_checkout_hint(self, page: Page) -> None:
        try:
            t = (await page.locator("body").inner_text(timeout=8_000)).lower()
        except Exception:
            return
        markers = (
            "carrito",
            "ir al carrito",
            "tu carrito",
            "checkout",
            "completar la compra",
            "finalizar compra",
            "complete your purchase",
            "your cart",
            "go to cart",
        )
        if any(m in t for m in markers):
            self._emit(
                "log",
                {
                    "message": (
                        "Lista partidos: el texto de la pagina sugiere carrito o checkout activo; "
                        "FIFA a veces no muestra el listado de partidos hasta vaciar el carrito o "
                        "concluir la compra pendiente. Pruebe vaciar carrito en el sitio y vuelva a "
                        "exportar session.json."
                    ),
                },
            )

    async def _emit_match_list_dom_diagnostic(self, page: Page) -> None:
        """Una muestra del DOM para depurar cuando no hay fila que cumpla criterios."""
        try:
            rows = await self._match_rows_locator(page)
            n = await rows.count()
            nf = len(self._fifa_product_list_frames(page))
            nfd = len(await self._fifa_product_list_frames_from_iframe_src(page))
            self._emit(
                "log",
                {
                    "message": (
                        f"Diagnostico lista partidos: filas={n} iframes_fifa_url={nf} "
                        f"iframes_desde_src={nfd} url={page.url[:140]} target_teams={self._target_team_ids()}"
                    ),
                },
            )
            if n == 0:
                await self._emit_match_list_cart_or_checkout_hint(page)
                sample: list[str] = []
                for fr in page.frames[:10]:
                    try:
                        fu = (fr.url or "")[:120]
                        if fu:
                            sample.append(fu)
                    except Exception:
                        continue
                if sample:
                    self._emit("log", {"message": f"  frame_urls_sample={sample!r}"})
            for i in range(min(5, n)):
                row = rows.nth(i)
                cls = (await row.get_attribute("class")) or ""
                host = await row.get_attribute("data-host-team-id") or await row.get_attribute(
                    "data-home-team-id"
                )
                guest = await row.get_attribute("data-opposing-team-id") or await row.get_attribute(
                    "data-away-team-id"
                )
                sold = self._row_is_sold_out(cls)
                self._emit(
                    "log",
                    {
                        "message": (
                            f"  [{i}] sold_out_hint={sold} class={cls[:100]!r} "
                            f"data-host-team-id={host!r} data-opposing-team-id={guest!r}"
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {"message": f"Diagnostico lista partidos fallo: {exc}"})

    async def _find_priority_match_row(self, page: Page) -> tuple[Locator, dict[str, Any]] | None:
        """Primera fila de partido: equipos (orden target_teams) y no agotada; precio se valida en tabla de categorías."""
        target_ids = self._target_team_ids()
        if not target_ids:
            raise RuntimeError("search_criteria.target_teams vacío en config.")

        rows = await self._match_rows_locator(page)
        n = await rows.count()
        for priority_tid in target_ids:
            tid = str(priority_tid).strip()
            for i in range(n):
                if self._stop.is_set():
                    return None
                row = rows.nth(i)
                try:
                    await row.scroll_into_view_if_needed(timeout=5_000)
                except Exception:
                    pass
                cls = await row.get_attribute("class") or ""
                if self._row_is_sold_out(cls):
                    continue
                host = await row.get_attribute("data-host-team-id") or await row.get_attribute(
                    "data-home-team-id"
                )
                guest = await row.get_attribute("data-opposing-team-id") or await row.get_attribute(
                    "data-away-team-id"
                )
                ids = {str(x).strip() for x in (host, guest) if x is not None and str(x).strip()}
                if tid not in ids:
                    continue
                if await self._row_has_hospitality_cta(row):
                    continue
                perf_id = await row.get_attribute("id")
                if not perf_id:
                    inner = row.locator("[id]").first
                    if await inner.count() > 0:
                        perf_id = await inner.get_attribute("id")
                return row, {
                    "target_team_id": priority_tid,
                    "host_team_id": host,
                    "opposing_team_id": guest,
                    "performance_id": perf_id,
                }
        return None

    async def _wait_url_contains_performance_table(self, page: Page, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            u = page.url.lower()
            if "/table/" in u and "/performance/" in u:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"Timeout esperando URL con /performance/.../table/: {page.url!r}")

    def _frames_for_tab_2_scan(self, page: Page) -> list[Frame]:
        """Orden: documento principal, luego iframes FIFA seat (sin captcha), resto."""
        ordered: list[Frame] = [page.main_frame]
        seat_iframes: list[Frame] = []
        rest: list[Frame] = []
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            u = self._frame_url_lower(fr)
            if self._is_datadome_or_captcha_frame_url(u):
                continue
            if "tickets.fifa.com" in u and "/selection/event/seat" in u:
                seat_iframes.append(fr)
            else:
                rest.append(fr)
        ordered.extend(seat_iframes)
        ordered.extend(rest)
        return ordered

    async def _click_tab_2_link_and_wait_table(self, page: Page, *, log_prefix: str) -> bool:
        """Clic en li#tab-2-link si existe (main o iframe); True si la URL llega a vista tabla."""
        clicked = False
        for fr in self._frames_for_tab_2_scan(page):
            tab_li = fr.locator("li#tab-2-link")
            try:
                if await tab_li.count() == 0:
                    continue
                await tab_li.first.wait_for(state="visible", timeout=12_000)
            except Exception:
                continue
            try:
                await tab_li.first.scroll_into_view_if_needed(timeout=10_000)
            except Exception:
                pass
            inner_link = tab_li.locator("a.title").first
            try:
                if await inner_link.count() > 0:
                    await inner_link.click(timeout=60_000)
                else:
                    await tab_li.first.click(timeout=60_000)
                clicked = True
                break
            except Exception as exc:
                self._emit(
                    "log",
                    {"message": f"{log_prefix}clic tab-2-link fallo: {exc}"},
                )
                return False

        if not clicked:
            self._emit(
                "log",
                {
                    "message": (
                        f"{log_prefix}tab-2-link no visible (main ni iframes FIFA). "
                        "Si la URL ya es /table/…, la grilla se abrio sin pestañas; si sigue en listado, "
                        "el clic en la fila no navego en el documento principal."
                    ),
                },
            )
            return False
        try:
            await self._wait_url_contains_performance_table(page, 90_000)
            self._emit(
                "log",
                {"message": f"{log_prefix}tras tab-2-link: {page.url[:140]}..."},
            )
            return True
        except TimeoutError:
            self._emit(
                "log",
                {"message": f"{log_prefix}timeout esperando /table/ tras tab-2-link (url={page.url[:120]}...)."},
            )
            return False

    async def _goto_shell_candidates_and_tab_link(self, page: Page, performance_id: str) -> bool:
        """Respaldo suave: goto shell ?productId&perfId&lang (sin table) + tab-2-link."""
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        pid = str(h.get("product_id", DEFAULT_PRODUCT_ID))
        lang = str(h.get("lang", "es"))
        query = f"productId={pid}&perfId={performance_id}&lang={lang}"
        shells = (
            f"{host}/secured/selection/event/seat?{query}",
            f"{host}/secure/selection/event/seat?{query}",
        )
        for shell in shells:
            try:
                self._emit(
                    "log",
                    {"message": f"Respaldo shell (sin table) + tab-2-link: goto → {shell[:130]}..."},
                )
                await page.goto(shell, wait_until="domcontentloaded", timeout=90_000)
                await self._jitter()
                if await self._click_tab_2_link_and_wait_table(page, log_prefix="[shell] "):
                    return True
            except Exception as exc:
                self._emit(
                    "log",
                    {"message": f"Respaldo shell fallo ({exc!s}); siguiente variante."},
                )
                continue
        return False

    async def _navigate_seat_via_mejor_sitio_tab_link(self, page: Page, row: Locator, performance_id: str) -> None:
        """
        Flujo humano: listado → clic fila → vista seat → tab-2-link → /table/N.
        Respaldos: shell query; goto directo canonico.
        """
        direct_table = self._seat_table_url(str(performance_id))
        seat_landing = self._seat_performance_landing_url(str(performance_id))

        self._emit(
            "log",
            {
                "message": (
                    "Flujo tab Mejor sitio: paso 1 — clic en enlace/fila del partido (listado) "
                    "hacia seat; si FIFA abre /table/ directo, no habra #tab-2-link."
                ),
            },
        )
        await self._click_match_row_for_seat_entry(page, row, performance_id)
        await self._post_navigation_blind_sleep_thread(log_context="Post-clic partido (DataDome / captcha)")

        deadline = time.monotonic() + 90.0
        seat_url_logged = False
        while time.monotonic() < deadline:
            u = page.url
            if self._seat_table_url_reached(u, performance_id):
                self._emit(
                    "log",
                    {
                        "message": (
                            "Tras clic en fila: URL ya es vista tabla (/performance/…/table/…). "
                            "Entrada directa sin pestaña «Mejor sitio»; se continua en esa vista."
                        ),
                    },
                )
                return
            if _SEAT_STEP_RE.search(u):
                if not seat_url_logged:
                    self._emit(
                        "log",
                        {"message": f"Tras clic en fila: url={u[:200]}{'...' if len(u) > 200 else ''}"},
                    )
                    seat_url_logged = True
                break
            await asyncio.sleep(0.2)
        else:
            self._emit(
                "log",
                {
                    "message": (
                        "Tras clic en fila la URL no llego a selection/event/seat en 90s; "
                        f"url_actual={page.url[:180]}… — respaldo shell ?perfId + tab-2-link."
                    ),
                },
            )
            if await self._goto_shell_candidates_and_tab_link(page, performance_id):
                return
            if bool(self._hunter_cfg().get("allow_direct_table_fallback", False)):
                await page.goto(direct_table, wait_until="domcontentloaded", timeout=90_000)
            else:
                msg = (
                    "No se logro abrir vista seat/tab de forma natural; se evita salto directo a /table "
                    "(hunter.allow_direct_table_fallback=false)."
                )
                self._emit("error", {"message": msg, "recoverable": True})
                raise SeatFlowNaturalEntryError(msg)
            return

        await self._jitter()
        if await self._click_tab_2_link_and_wait_table(page, log_prefix="[tras fila] "):
            return

        self._emit(
            "log",
            {
                "message": (
                    "tab-2-link no aparecio tras clic en fila; intento URL intermedia "
                    f"seat/performance/lang antes de usar /table: {seat_landing[:130]}..."
                ),
            },
        )
        try:
            await page.goto(seat_landing, wait_until="domcontentloaded", timeout=90_000)
            await self._jitter()
            if await self._click_tab_2_link_and_wait_table(page, log_prefix="[landing] "):
                return
        except Exception as exc:
            self._emit("log", {"message": f"URL intermedia seat/performance/lang fallo ({exc!s}); siguiendo respaldos."})

        self._emit(
            "log",
            {
                "message": (
                    "#tab-2-link no funciono tras clic fila (mapa o pestaña distinta); "
                    "respaldo goto shell + tab-2-link."
                ),
            },
        )
        if await self._goto_shell_candidates_and_tab_link(page, performance_id):
            return

        if bool(self._hunter_cfg().get("allow_direct_table_fallback", False)):
            self._emit(
                "log",
                {"message": "tab-2-link (todos los intentos): fallback goto directo /table/N canonico."},
            )
            await page.goto(direct_table, wait_until="domcontentloaded", timeout=90_000)
        else:
            msg = (
                "tab-2-link no disponible tras todos los intentos; se evita salto directo /table "
                "(hunter.allow_direct_table_fallback=false) para mantener flujo natural."
            )
            self._emit("error", {"message": msg, "recoverable": True})
            raise SeatFlowNaturalEntryError(msg)

    async def _open_match_row(self, page: Page, row: Locator, meta: dict[str, Any]) -> None:
        perf_id = meta.get("performance_id")
        if not perf_id:
            raise RuntimeError("performance_id faltante en fila de partido (id del li.performance).")
        self._emit(
            "log",
            {
                "message": (
                    f"Partido elegido: perfId={perf_id} "
                    f"equipo objetivo={meta.get('target_team_id')} "
                    f"(local={meta.get('host_team_id')} visita={meta.get('opposing_team_id')})."
                ),
            },
        )
        use_map = bool(self._hunter_cfg().get("use_seat_map_entry", False))
        if use_map:
            await row.scroll_into_view_if_needed()
            await row.click(timeout=45_000)
            await self._post_navigation_blind_sleep_thread(log_context="Mapa: post-clic partido (DataDome / captcha)")
            await self._wait_url_matches(page, _SEAT_STEP_RE, 90_000, "selection/event/seat")
            return

        if bool(self._hunter_cfg().get("seat_entry_via_tab_link", False)):
            await self._navigate_seat_via_mejor_sitio_tab_link(page, row, str(perf_id))
            return

        target = self._seat_table_url(str(perf_id))
        self._emit(
            "log",
            {
                "message": (
                    "Entrada directa a vista tabla (evita mapa): "
                    f"{target[:120]}..."
                ),
            },
        )
        await page.goto(target, wait_until="domcontentloaded", timeout=90_000)

    async def _maybe_click_mejor_sitio(self, page: Page) -> None:
        """Solo en flujo con mapa; en /table/N la UI ya suele estar en la vista rápida."""
        if "/table/" in page.url.lower():
            self._emit("log", {"message": "Ruta /table/: se omite clic en Mejor sitio."})
            return
        btn = page.get_by_text(re.compile(r"(reservar\s+el\s+)?mejor\s+sitio", re.I))
        if await btn.count() == 0:
            self._emit("log", {"message": "Sin boton Mejor sitio; se continua el flujo."})
            return
        await btn.first.click(timeout=60_000)

    def _seat_ui_root_url(self, root: SeatUiRoot) -> str:
        try:
            u = getattr(root, "url", "") or ""
            return str(u)[:140]
        except Exception:
            return ""

    @staticmethod
    def _frame_url_lower(fr: Frame) -> str:
        try:
            return (fr.url or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _is_datadome_or_captcha_frame_url(url: str) -> bool:
        u = (url or "").lower()
        return "captcha-delivery.com" in u or "geo.captcha" in u or "datadome.co" in u

    def _fifa_seat_content_frame(self, page: Page) -> Frame | None:
        """
        En /table/1/lang/es el documento principal suele ser un shell; la grilla vive en un iframe
        cuya URL contiene /selection/event/seat/.../performance/.../table (sin captcha).
        """
        fallback: Frame | None = None
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            u = self._frame_url_lower(fr)
            if not u or self._is_datadome_or_captcha_frame_url(u):
                continue
            if "tickets.fifa.com" not in u or "/selection/event/seat" not in u:
                continue
            if "/performance/" not in u:
                continue
            fallback = fr
            if u.rstrip("/").endswith("/table") or "/table/" in u:
                return fr
        return fallback

    @staticmethod
    def _urls_loosely_match_iframe_src(expected_abs: str, frame_url: str) -> bool:
        if not frame_url or not expected_abs:
            return False
        e = expected_abs.lower().split("?")[0].rstrip("/")
        f = frame_url.lower().split("?")[0].rstrip("/")
        if e == f:
            return True
        return f in e or e in f

    async def _fifa_seat_frame_from_iframe_element(self, page: Page) -> Frame | None:
        """
        Si Frame.url llega vacío o tarde, enlazar por el <iframe src=...> del shell principal.
        Nunca devolver el iframe de DataDome/captcha (src genérico "performance" podía emparejar mal).
        """
        for css in (
            'iframe[src*="selection/event/seat"]',
            'iframe[src*="/event/seat/performance"]',
            'iframe[src*="/performance/"]',
        ):
            el = page.locator(css).first
            if await el.count() == 0:
                continue
            raw = (await el.get_attribute("src")) or ""
            if not raw.strip():
                continue
            abs_src = urljoin(page.url, raw.strip()).lower()
            if "captcha-delivery" in abs_src or "geo.captcha" in abs_src:
                continue
            if "tickets.fifa.com" not in abs_src:
                continue
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                fu = fr.url or ""
                if self._is_datadome_or_captcha_frame_url(fu):
                    continue
                if self._urls_loosely_match_iframe_src(abs_src, fu):
                    return fr
            for _ in range(25):
                await asyncio.sleep(0.12)
                for fr in page.frames:
                    if fr == page.main_frame:
                        continue
                    fu = fr.url or ""
                    if self._is_datadome_or_captcha_frame_url(fu):
                        continue
                    if self._urls_loosely_match_iframe_src(abs_src, fu):
                        return fr
        return None

    async def _fifa_seat_content_frame_resolved(self, page: Page) -> Frame | None:
        hit = self._fifa_seat_content_frame(page)
        if hit is not None:
            return hit
        return await self._fifa_seat_frame_from_iframe_element(page)

    def _datadome_captcha_iframe_present(self, page: Page) -> bool:
        for fr in page.frames:
            if self._is_datadome_or_captcha_frame_url(fr.url or ""):
                return True
        return False

    async def _detect_seat_ui_root_once(self, page: Page) -> SeatUiRoot:
        """
        FIFA suele renderizar la tabla de categorías dentro de un iframe; el documento principal
        puede quedar vacío de <select> / [data-amount] (diagnostico previo: count=0 en page).
        """
        best: SeatUiRoot | None = None
        best_score = -1
        candidates: list[SeatUiRoot] = [page, *[f for f in page.frames if f != page.main_frame]]
        for fr in candidates:
            if isinstance(fr, Frame) and self._is_datadome_or_captcha_frame_url(fr.url or ""):
                continue
            try:
                ns = await fr.locator("select").count()
                na = await fr.locator("[data-amount]").count()
                tr_hit = await fr.locator("tr:has(select):has([data-amount])").count()
            except Exception:
                continue
            score = 0
            if tr_hit > 0:
                score += 20
            if ns > 0 and na > 0:
                score += 10
            elif ns > 0 or na > 0:
                score += 1
            if score > best_score:
                best_score = score
                best = fr
        if best_score >= 10 and best is not None:
            return best
        fifa_if = await self._fifa_seat_content_frame_resolved(page)
        if fifa_if is not None:
            return fifa_if
        return best if best is not None else page

    async def _ensure_seat_category_ui(self, page: Page) -> SeatUiRoot:
        """Sondeo ligero (sin esperar 60s+25s en selectores del main vacío)."""
        fifa_if = await self._fifa_seat_content_frame_resolved(page)
        if fifa_if is not None:
            try:
                await fifa_if.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass

        raw_to = self._hunter_cfg().get("seat_category_ui_timeout_sec", 45)
        try:
            timeout_sec = max(5.0, float(raw_to))
        except (TypeError, ValueError):
            timeout_sec = 45.0
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            root = await self._detect_seat_ui_root_once(page)
            try:
                if await root.locator("tr:has(select):has([data-amount])").count() > 0:
                    return root
                if await root.locator("table:has(select):has([data-amount])").count() > 0:
                    return root
                ns = await root.locator("select").count()
                na = await root.locator("[data-amount]").count()
                if ns > 0 and na > 0:
                    return root
            except Exception:
                pass
            await asyncio.sleep(0.35)
        return await self._detect_seat_ui_root_once(page)

    async def _locate_category_table_rows(self, root: SeatUiRoot) -> Locator:
        """
        Filas con combo + precio; primero sin depender de <table> (layouts raros), luego tabla acotada.
        """
        loose = root.locator("tr:has(select):has([data-amount])")
        if await loose.count() > 0:
            return loose
        m = root.locator("table:has(select):has([data-amount])")
        if await m.count() > 0:
            table = m.first
            inner = table.locator("tbody tr")
            if await inner.count() > 0:
                return inner
            return table.locator("tr")
        m2 = root.locator("table:has(select)")
        table = m2.first if await m2.count() > 0 else None
        if table is None:
            body = root.locator("table tbody tr")
            if await body.count() > 0:
                return body
            return root.locator("table tr")
        inner = table.locator("tbody tr")
        if await inner.count() > 0:
            return inner
        return table.locator("tr")

    async def _row_quantity_select(self, row: Locator) -> Locator | None:
        spec = row.locator(
            'select[id*="quantity"], select[name*="quantity"], '
            'select[id*="Quantity"], select[name*="Quantity"], '
            'select[id*="cantidad"], select[name*="cantidad"]'
        )
        if await spec.count() > 0:
            return spec.first
        selects = row.locator("select")
        ns = await selects.count()
        for i in range(ns):
            sel = selects.nth(i)
            if await self._select_looks_like_ticket_quantity(sel):
                return sel
        return None

    async def _select_looks_like_ticket_quantity(self, sel: Locator) -> bool:
        opts = sel.locator("option")
        oc = await opts.count()
        hits = 0
        for j in range(min(oc, 16)):
            opt = opts.nth(j)
            v = await opt.get_attribute("value")
            if v is None or str(v).strip() == "":
                continue
            try:
                q = int(str(v).strip())
            except ValueError:
                continue
            if 1 <= q <= 10 and await opt.get_attribute("disabled") is None:
                hits += 1
        return hits >= 1

    async def _emit_category_table_pick_failure_diagnostic(
        self, page: Page, root: SeatUiRoot | None = None
    ) -> None:
        try:
            root = root or await self._detect_seat_ui_root_once(page)
            t_main = await root.locator("table:has(select):has([data-amount])").count()
            t_sel = await root.locator("table:has(select)").count()
            tr_loose = await root.locator("tr:has(select):has([data-amount])").count()
            n_sel = await root.locator("select").count()
            n_amt = await root.locator("[data-amount]").count()
            rows = await self._locate_category_table_rows(root)
            n = await rows.count()
            frame_urls = []
            for fr in page.frames[:12]:
                try:
                    fu = (fr.url or "")[:100]
                    if fu:
                        frame_urls.append(fu)
                except Exception:
                    continue
            dd = self._datadome_captcha_iframe_present(page)
            self._emit(
                "log",
                {
                    "message": (
                        f"Diagnostico tabla categorias: root={self._seat_ui_root_url(root)!r} "
                        f"tr(sel+amt)={tr_loose} tables(select+amount)={t_main} tables(select)={t_sel} "
                        f"root_selects={n_sel} root_data_amount={n_amt} filas_en_scope={n} "
                        f"datadome_captcha_iframe={dd} frames={len(page.frames)} "
                        f"page_url={page.url[:120]}"
                    ),
                },
            )
            if frame_urls:
                self._emit("log", {"message": f"  iframe_urls_sample={frame_urls!r}"})
            for i in range(min(4, n)):
                r = rows.nth(i)
                try:
                    snippet = (await r.inner_text())[:160].replace("\n", " ")
                except Exception as exc:
                    snippet = f"<error {exc}>"
                hs = await self._row_quantity_select(r) is not None
                ha = await r.locator("[data-amount]").count() > 0
                cn = await self._row_category_number(r)
                un = await self._category_table_row_unavailable(r)
                self._emit(
                    "log",
                    {
                        "message": (
                            f"  fila[{i}] unavailable={un} cat={cn} has_select={hs} "
                            f"has_data_amount={ha} text={snippet!r}"
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self._emit("log", {"message": f"Diagnostico tabla categorias fallo: {exc}"})

    async def _category_table_row_unavailable(self, row: Locator) -> bool:
        if await row.locator("div.category_unavailable_overlay").count() > 0:
            return True
        try:
            text = (await row.inner_text()).strip()
        except Exception:
            return False
        lo = text.lower()
        if "actualmente no disponible" in lo:
            return True
        if "not currently available" in lo:
            return True
        return False

    async def _row_category_number(self, row: Locator) -> int | None:
        cell0 = row.locator("th, td").first
        if await cell0.count() > 0:
            try:
                t0 = await cell0.inner_text()
            except Exception:
                t0 = ""
            cn = self._category_number_from_label_text(t0)
            if cn is not None:
                return cn
        try:
            whole = await row.inner_text()
        except Exception:
            return None
        return self._category_number_from_label_text(whole)

    async def _pick_category_table_row_and_quantity(self, page: Page) -> dict[str, Any] | None:
        """
        Tabla de categorías: primera celda th|td con leyenda (Categoría N, Zona… (categoría N), …);
        fila no vendible: overlay category_unavailable_overlay o texto «Actualmente no disponible».
        Select de cantidad y span.amount[data-amount] se buscan en toda la fila (orden de columnas variable).
        Prioridad: preferred_categories. Cantidad: máximo valor de option <= quantity en config.
        """
        root = await self._ensure_seat_category_ui(page)
        self._last_seat_ui_root = root
        if root is not page:
            self._emit(
                "log",
                {
                    "message": (
                        "Tabla de categorias: UI de asientos en subframe (iframe), "
                        f"no en el documento principal. url_frame={self._seat_ui_root_url(root)}"
                    ),
                },
            )

        raw_pref = self._criteria().get("preferred_categories") or [1, 2, 3, 4]
        preferred: list[int] = []
        for x in raw_pref:
            try:
                preferred.append(int(x))
            except (TypeError, ValueError):
                continue
        if not preferred:
            preferred = [1, 2, 3, 4]
        qty_limit = max(1, int(self._criteria().get("quantity", 1)))
        table_rows = await self._locate_category_table_rows(root)
        n = await table_rows.count()
        if n == 0 and self._datadome_captcha_iframe_present(page):
            self._emit(
                "error",
                {
                    "message": (
                        "FIFA/DataDome: iframe captcha-delivery visible y 0 filas de categorias en el "
                        "frame de asientos; la grilla no llega a montarse sin validacion humana. "
                        "Headless no puede resolver este paso: evite hunter+Chrome a la vez, renueve "
                        "session tras resolver captcha en navegador visible, o pruebe con flujo solo CDP."
                    ),
                    "recoverable": True,
                },
            )
            self._emit_captcha_handoff_required(page, step="seat_category_table")
            raise SeatFlowBlockedByCaptchaError("DataDome bloquea la vista de categorias en asientos.")

        for cat_priority in preferred:
            if self._stop.is_set():
                return None
            for i in range(n):
                row = table_rows.nth(i)
                if await self._category_table_row_unavailable(row):
                    continue
                cn = await self._row_category_number(row)
                if cn != cat_priority:
                    continue

                sel = await self._row_quantity_select(row)
                if sel is None:
                    continue

                try:
                    row_ctx = await row.inner_text()
                except Exception:
                    row_ctx = None

                price_el = row.locator("span.amount[data-amount], span[data-amount]").first
                hint = await self._price_from_amount_element(price_el, context_text=row_ctx)
                if hint is None:
                    hint = await self._price_from_amount_element(
                        row.locator("[data-amount]").first, context_text=row_ctx
                    )
                if hint is None:
                    self._emit("log", {"message": f"Tabla: categoria {cn} sin precio; siguiente fila."})
                    continue
                if not self._price_within_budget(hint):
                    self._emit(
                        "log",
                        {
                            "message": (
                                f"Tabla: categoria {cn} precio fuera de max_price_cents "
                                f"({hint.get('amount')} {hint.get('currency')}); siguiente."
                            ),
                        },
                    )
                    continue

                opts = sel.locator("option")
                oc = await opts.count()
                numeric_values: list[int] = []
                for j in range(oc):
                    opt = opts.nth(j)
                    v = await opt.get_attribute("value")
                    if v is None or str(v).strip() == "":
                        continue
                    try:
                        q = int(str(v).strip())
                    except ValueError:
                        continue
                    if q < 0:
                        continue
                    if await opt.get_attribute("disabled") is not None:
                        continue
                    numeric_values.append(q)

                candidates = [q for q in numeric_values if q <= qty_limit]
                if not candidates:
                    self._emit(
                        "log",
                        {"message": f"Tabla: categoria {cn} sin opcion de cantidad <= {qty_limit}; siguiente."},
                    )
                    continue
                chosen = max(candidates)

                await sel.scroll_into_view_if_needed()
                await sel.select_option(value=str(chosen))
                await sel.evaluate(
                    """el => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
                self._emit(
                    "log",
                    {
                        "message": (
                            f"Tabla categorias: categoria {cn}, cantidad {chosen}, "
                            f"precio OK ({hint.get('amount')} {hint.get('currency')})."
                        ),
                    },
                )
                return {"price_hint": hint, "quantity": chosen, "category": cn}

        await self._emit_category_table_pick_failure_diagnostic(page, root)
        return None

    async def _humanized_click_book(self, page: Page) -> None:
        root = self._last_seat_ui_root or await self._detect_seat_ui_root_once(page)
        primary = root.locator("a#book")
        if await primary.count() > 0:
            target = primary.first
        else:
            candidates = root.locator('a[id^="book"]')
            n = await candidates.count()
            target = None
            for i in range(n):
                el = candidates.nth(i)
                try:
                    txt = (await el.inner_text()).lower()
                    href = ((await el.get_attribute("href")) or "").lower()
                except Exception:
                    continue
                if "hospitality" in txt or "hospitality" in href:
                    continue
                target = el
                break
            if target is None:
                target = root.locator("#book").first
        await target.wait_for(state="visible", timeout=90_000)
        box = await target.bounding_box()
        if not box:
            await target.click(timeout=30_000)
            return
        pad_x = min(12.0, max(4.0, box["width"] * 0.15))
        pad_y = min(10.0, max(3.0, box["height"] * 0.2))
        x = box["x"] + random.uniform(pad_x, max(pad_x + 1.0, box["width"] - pad_x))
        y = box["y"] + random.uniform(pad_y, max(pad_y + 1.0, box["height"] - pad_y))
        await page.mouse.click(x, y)

    async def _page_needs_auth(self, page: Page) -> bool:
        u = page.url.lower()
        return "login" in u and FIFA_HOST in u

    async def _page_fifa_bot_wall(self, page: Page) -> bool:
        try:
            body = (await page.locator("body").inner_text(timeout=8_000)).lower()
        except Exception:
            return False
        markers = (
            "este bloqueo",
            "sobrehumana",
            "sobrehumano",
            "un robot",
            "misma red",
            "dificultades para acceder",
            "restringido temporalmente",
            "acceso está restringido",
            "acceso esta restringido",
            "comportamiento inusual",
            "lamentamos las molestias",
            "temporarily restricted",
        )
        return any(m in body for m in markers)

    _STEP_ID_SAFE_RE = re.compile(r"[^a-z0-9_]+", re.I)

    async def _headless_debug_checkpoint(
        self,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        p: Playwright,
        *,
        step_id: str,
        note_es: str,
    ) -> tuple[Browser, BrowserContext, Page]:
        """
        Modo diagnóstico: tras cada hito, registra URL, bot_wall, iframe DataDome,
        guarda un snapshot de storage_state y pausa para inspección manual.
        Activa con hunter.headless_step_debug: true en config.yaml.
        Tras la pausa, reabre Playwright si el Dashboard cerro Chromium para validar en Chrome.
        """
        if not bool(self._hunter_cfg().get("headless_step_debug", False)):
            return browser, context, page
        h = self._hunter_cfg()
        try:
            pause_sec = float(h.get("headless_step_pause_sec", 90))
        except (TypeError, ValueError):
            pause_sec = 90.0
        save_snaps = bool(h.get("headless_step_save_snapshots", True))

        safe = self._STEP_ID_SAFE_RE.sub("_", step_id.lower()).strip("_") or "step"
        snap_path: Path | None = None
        if save_snaps:
            snap_path = self.project_root / f"session_snapshot_{safe}.json"
            try:
                await context.storage_state(path=str(snap_path))
            except Exception as exc:  # noqa: BLE001
                snap_path = None
                self._emit(
                    "log",
                    {"message": f"HEADLESS CHECKPOINT [{step_id}]: no se pudo guardar snapshot ({exc})."},
                )

        wall = await self._page_fifa_bot_wall(page)
        dd = self._datadome_captcha_iframe_present(page)

        url = page.url
        url_short = url if len(url) <= 140 else f"{url[:137]}..."

        wait_ui = self._debug_continue_event is not None
        payload: dict[str, Any] = {
            "step_id": step_id,
            "note": note_es,
            "url": url,
            "bot_wall": wall,
            "datadome_iframe_visible": dd,
            "session_snapshot": str(snap_path) if snap_path else None,
            "pause_sec": pause_sec,
            "wait_for_ui_continue": wait_ui,
        }
        self._emit("hunter_checkpoint", payload)

        wall_es = "SI" if wall else "no"
        dd_es = "si" if dd else "no"
        snap_es = str(snap_path) if snap_path else "(sin archivo)"
        if wait_ui:
            wait_hint = (
                f"esperando «Continuar hunter» en el dashboard"
                + (f" (tope {pause_sec}s)" if pause_sec > 0 else " (sin tope de tiempo)")
            )
        else:
            wait_hint = f"pausa {pause_sec}s" if pause_sec > 0 else "sin pausa"
        self._emit(
            "log",
            {
                "message": (
                    f"HEADLESS CHECKPOINT [{step_id}] {note_es} | url={url_short} | "
                    f"bot_wall={wall_es} | datadome_iframe={dd_es} | snapshot={snap_es} | "
                    f"{wait_hint} (desactive con hunter.headless_step_debug: false)."
                ),
            },
        )
        if dd:
            self._emit(
                "log",
                {
                    "message": (
                        f"HEADLESS CHECKPOINT [{step_id}]: iframe DataDome detectado. "
                        "Headless no puede resolverlo aqui; si tras Continuar falla el flujo, "
                        "detenga el hunter, resuelva el reto solo en Chrome CDP (sin headless), "
                        "vuelva a capturar session.json y reintente (o desactive hasta paso asientos)."
                    ),
                },
            )

        resume_url = page.url
        evt = self._debug_continue_event
        if evt is not None:
            evt.clear()
            if pause_sec > 0:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=pause_sec)
                except asyncio.TimeoutError:
                    self._emit(
                        "log",
                        {
                            "message": (
                                f"HEADLESS CHECKPOINT [{step_id}]: vencio el tope de {pause_sec}s; "
                                "continua sin pulsar Continuar."
                            ),
                        },
                    )
            else:
                await evt.wait()
        elif pause_sec > 0:
            await asyncio.sleep(pause_sec)

        return await self._reopen_playwright_if_needed(p, resume_url=resume_url)

    def _app_chrome_profile_dir(self) -> str | None:
        raw = (self.config.get("app") or {}).get("biting_lobster_chrome_profile")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().strip('"').strip("'")
        return None

    async def _pre_seat_browser_validation_gate(
        self,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        p: Playwright,
        meta: dict[str, Any],
    ) -> tuple[Browser, BrowserContext, Page]:
        """
        Pausa antes de abrir partido / vista asientos (un paso antes de donde suele aparecer DataDome).
        Persiste session, emite evento para UI (Chrome con perfil CDP) y espera Continuar en dashboard.
        """
        if not bool(self._hunter_cfg().get("pre_seat_visual_validation", False)):
            return browser, context, page
        if bool(self._hunter_cfg().get("headless_step_debug", False)) and bool(
            self._hunter_cfg().get("headless_step_single_list_pause", True)
        ):
            self._emit(
                "log",
                {
                    "message": (
                        "pre_seat_visual_validation omitido: headless_step_single_list_pause=true "
                        "ya deja una sola pausa en listado."
                    ),
                },
            )
            return browser, context, page
        try:
            await context.storage_state(path=str(self.session_path))
        except Exception:
            pass

        raw_to = self._hunter_cfg().get("pre_seat_validation_timeout_sec", 600)
        try:
            timeout_sec = float(raw_to)
        except (TypeError, ValueError):
            timeout_sec = 600.0

        perf = str(meta.get("performance_id") or "?")
        wall = await self._page_fifa_bot_wall(page)
        dd = self._datadome_captcha_iframe_present(page)
        profile = self._app_chrome_profile_dir()

        self._emit(
            "pre_seat_browser_validation",
            {
                "list_url": page.url,
                "performance_id": perf,
                "timeout_sec": timeout_sec,
                "bot_wall_headless": wall,
                "datadome_iframe_headless": dd,
                "chrome_user_data_dir": profile,
                "instructions_es": (
                    "El listado esta en pausa (aun no se abre la tabla de asientos).\n\n"
                    "Use en el Dashboard «Chrome CDP — validar pausa» o «Chrome normal — validar pausa»: "
                    "la app cierra primero el Chromium de Playwright para evitar sesion paralela y falsos positivos.\n\n"
                    "Chrome normal no usa session.json (perfil del sistema): ver login o captchas extra es normal.\n"
                    "Chrome CDP usa la carpeta del onboarding: si acaba de recrearse el perfil vacio, la primera vez "
                    "parecera instalacion nueva hasta que entre a FIFA.\n\n"
                    "Tras validar, pulse «Continuar hunter» para reabrir Playwright con session.json y seguir.\n\n"
                    "Si el listado ya esta bloqueado, detenga el hunter y renueve session.json."
                ),
            },
        )
        self._emit(
            "log",
            {
                "message": (
                    "VALIDACION PRE-ASIENTOS: pausa en el listado. "
                    f"URL={page.url[:120]}{'...' if len(page.url) > 120 else ''} | "
                    f"perfId={perf} | bot_wall(headless)={'SI' if wall else 'no'} | "
                    f"datadome_iframe(headless)={'si' if dd else 'no'} | "
                    f"tope_espera_s={timeout_sec}. Use Chrome desde el Dashboard (cierra Playwright antes) o el dialogo; "
                    "luego «Continuar hunter»."
                ),
            },
        )

        resume_url = page.url
        evt = self._debug_continue_event
        if evt is None:
            self._emit(
                "log",
                {
                    "message": (
                        "pre_seat_visual_validation: sin boton Continuar (ej. scripts/run_hunter.py); "
                        f"espera {min(timeout_sec, 120.0)}s y continua."
                    ),
                },
            )
            await asyncio.sleep(min(timeout_sec, 120.0) if timeout_sec > 0 else 60.0)
            return await self._reopen_playwright_if_needed(p, resume_url=resume_url)

        evt.clear()
        if timeout_sec > 0:
            try:
                await asyncio.wait_for(evt.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                self._emit(
                    "log",
                    {
                        "message": (
                            "pre_seat_visual_validation: vencio el tiempo de espera; "
                            "el hunter continua hacia la vista de asientos."
                        ),
                    },
                )
        else:
            await evt.wait()

        return await self._reopen_playwright_if_needed(p, resume_url=resume_url)

    async def run_loop(self) -> None:
        if not self.session_path.is_file():
            self._emit("error", {"message": f"No existe {self.session_path}", "recoverable": False})
            return

        ok, prereq_msg = validate_hunter_search_objective(self.config)
        if not ok:
            self._emit("error", {"message": prereq_msg, "recoverable": False})
            return

        if self._hunter_attach_chrome_cdp() and self._use_camoufox():
            self._emit(
                "error",
                {
                    "message": "attach_hunter_to_chrome_cdp no es compatible con use_camoufox: desactive uno de los dos.",
                    "recoverable": False,
                },
            )
            return

        self._stop.clear()
        self._match_list_diagnostic_emitted = False

        pw_vis = "headless" if self._playwright_headless_launch_arg() else "ventana visible (playwright_headless: false)"
        if self._hunter_attach_chrome_cdp():
            pw_vis = "Chrome CDP (connect_over_cdp; pestaña existente)"
            if bool(self._hunter_cfg().get("playwright_headless", True)):
                self._emit(
                    "log",
                    {
                        "message": (
                            "attach_hunter_to_chrome_cdp: Chrome ya esta en pantalla; "
                            "playwright_headless se ignora en este modo."
                        ),
                    },
                )
        backend_desc = (
            "Camoufox (Firefox) sin playwright-stealth"
            if self._use_camoufox()
            else (
                "Chrome CDP vivo (connect_over_cdp; sin new_context; stealth en pestaña FIFA)"
                if self._hunter_attach_chrome_cdp()
                else "Chromium + playwright-stealth"
            )
        )
        self._emit(
            "log",
            {
                "message": (
                    f"HunterService: Playwright {pw_vis}; backend={backend_desc}; "
                    f"session.json; domcontentloaded only; jitter={self._normalized_speed_key()}."
                ),
            },
        )
        if bool(self._hunter_cfg().get("headless_step_debug", False)):
            self._emit(
                "log",
                {
                    "message": (
                        "Modo headless_step_debug activo: el hunter pausara tras cada checkpoint, "
                        "guardara session_snapshot_<paso>.json y registrara bot_wall/datadome. "
                        "No hace falta abrir Chrome en cada pausa: basta con «Continuar hunter». "
                        "Evite usar la misma sesion en Chrome visible en paralelo al headless (riesgo anti-fraude). "
                        + (
                            "Use el boton «Continuar hunter» en el dashboard para avanzar cada paso."
                            if self._debug_continue_event is not None
                            else "Sin UI: usa headless_step_pause_sec o scripts/run_hunter.py sin boton."
                        )
                    ),
                },
            )
        if bool(self._hunter_cfg().get("pre_seat_visual_validation", False)):
            self._emit(
                "log",
                {
                    "message": (
                        "pre_seat_visual_validation activo: antes de abrir el partido el hunter pausara, "
                        "guardara session.json y podra abrir el listado en Chrome (perfil CDP) para comprobar "
                        "que no hay bloqueo; luego pulse «Continuar hunter»."
                    ),
                },
            )

        attach = self._hunter_attach_chrome_cdp()
        ua_capture = self._load_chrome_cdp_saved_user_agent() if not attach else None
        self._chromium_stealth_plugin = None
        if not self._use_camoufox():
            if attach:
                self._chromium_stealth_plugin = Stealth()
                self._emit(
                    "log",
                    {
                        "message": (
                            "Hunter attach CDP: sin new_context; Stealth en la pestaña FIFA existente "
                            "(navigator.userAgent nativo de Chrome)."
                        ),
                    },
                )
            else:
                sk_ua: dict[str, Any] = {}
                if ua_capture:
                    sk_ua["navigator_user_agent_override"] = ua_capture
                self._chromium_stealth_plugin = Stealth(**sk_ua)
                if ua_capture:
                    self._emit(
                        "log",
                        {
                            "message": (
                                "Hunter Chromium: User-Agent HTTP + stealth alineados con Chrome CDP "
                                f"({ua_capture[:100]}{'...' if len(ua_capture) > 100 else ''})"
                            ),
                        },
                    )
                else:
                    self._emit(
                        "log",
                        {
                            "message": (
                                "Hunter Chromium: falta session_chrome_user_agent.txt junto a session.json; "
                                "la proxima captura CDP guardara navigator.userAgent. Viewport evasion 1920x1080 "
                                "(override: hunter.chromium_stealth_viewport)."
                            ),
                        },
                    )

        try:
            async with async_playwright() as p:
                if attach:
                    browser = await self._connect_hunter_over_cdp_browser(p)
                    context, page = self._attach_pick_existing_fifa_page(browser)
                    await self._apply_chromium_stealth_to_page(page)
                    await self._apply_proxy_bandwidth_routes_if_enabled(page)
                    self._sync_playwright_session_refs(browser, context, page)
                    self._emit(
                        "log",
                        {
                            "message": (
                                f"Hunter attach CDP: pestaña activa URL={page.url[:140]}"
                                f"{'...' if len(page.url) > 140 else ''} (sin crear contexto nuevo)."
                            ),
                        },
                    )
                else:
                    browser = await self._launch_playwright_browser(p)
                    if self._use_camoufox():
                        ctx_kw: dict[str, Any] = {
                            "storage_state": str(self.session_path),
                            "locale": "es-MX",
                            "ignore_https_errors": self._playwright_ignore_https_errors(),
                        }
                        if self._camoufox_auto_fingerprint_screen():
                            self._emit("log", {"message": "Camoufox: auto_fingerprint_screen activo (sin viewport fijo)."})
                        else:
                            ctx_kw["viewport"] = self._playwright_viewport_size()
                    else:
                        ctx_kw = {
                            "storage_state": str(self.session_path),
                            "locale": "es-MX",
                            "ignore_https_errors": self._playwright_ignore_https_errors(),
                            "viewport": self._chromium_stealth_viewport(),
                        }
                        if ua_capture:
                            ctx_kw["user_agent"] = ua_capture
                    ctx_kw.update(self._chromium_context_proxy_kw())
                    context = await browser.new_context(**ctx_kw)
                    page = await context.new_page()
                    await self._apply_chromium_stealth_to_page(page)
                    await self._apply_proxy_bandwidth_routes_if_enabled(page)
                    self._sync_playwright_session_refs(browser, context, page)
                if self._use_camoufox():
                    if self._playwright_ignore_https_errors():
                        self._emit(
                            "log",
                            {"message": "Playwright: ignore_https_errors=true (TLS del proxy no bloquea navegación)."},
                        )
                    self._emit(
                        "log",
                        {
                            "message": (
                                "Camoufox (Firefox): session.json suele venir de Chrome CDP. FIFA puede mostrar "
                                "«acceso restringido» o pedir login al instante (huella distinta; "
                                "DataDome revalida). Pruebe: cerrar todo otro navegador con la misma cuenta, "
                                "esperar enfriamiento, subir hunter.initial_delay_sec, o abrir la tienda una "
                                "vez a mano en Camoufox y volver a guardar session si dispone de flujo para eso. "
                                "Si persiste, use use_camoufox: false con Chromium hasta tener flujo estable."
                            ),
                        },
                    )

                init_delay = float(self._hunter_cfg().get("initial_delay_sec", 3.5))
                if init_delay > 0:
                    await asyncio.sleep(init_delay)

                list_url = await self._enter_match_list_page(p, page)

                if _coerce_bool(self._hunter_cfg().get("team_filter_probe_only", False)):
                    await self._run_team_filter_probe_only(page, list_url)
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    self._sync_playwright_session_refs(None, None, None)
                    return

                browser, context, page = await self._headless_debug_checkpoint(
                    browser,
                    context,
                    page,
                    p,
                    step_id="after_match_list_entry",
                    note_es="Tras navegar a listado (tienda+COMPRAR BOLETOS o skip_secured_content).",
                )
                if await self._page_needs_auth(page):
                    self._emit("auth_required", {"url": page.url})
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    self._sync_playwright_session_refs(None, None, None)
                    return
                if await self._page_fifa_bot_wall(page):
                    self._emit(
                        "error",
                        {
                            "message": (
                                "FIFA: pantalla de bloqueo / anti-bot detectada. Suele pasar si Chrome de "
                                "captura sigue abierto con la misma sesion, o si abres la misma URL en dos "
                                "navegadores a la vez. Cierra Chrome, espera unos minutos, vuelve a capturar "
                                "session.json si hace falta y reintenta (sube hunter.initial_delay_sec si persiste)."
                            ),
                            "recoverable": True,
                            "url": page.url,
                        },
                    )
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    self._sync_playwright_session_refs(None, None, None)
                    return

                single_list_pause = bool(self._hunter_cfg().get("headless_step_single_list_pause", True))
                if single_list_pause and bool(self._hunter_cfg().get("headless_step_debug", False)):
                    self._emit(
                        "log",
                        {
                            "message": (
                                "headless_step_single_list_pause activo: en listado se mantiene una sola pausa "
                                "(after_match_list_entry); se omiten after_match_list_populated y before_open_match."
                            ),
                        },
                    )

                try:
                    await self._wait_for_match_list_populated(page)
                except Exception as exc:  # noqa: BLE001
                    self._emit(
                        "log",
                        {"message": f"Lista partidos: grilla no lista aun ({exc}); se intenta el bucle."},
                    )

                if not (single_list_pause and bool(self._hunter_cfg().get("headless_step_debug", False))):
                    browser, context, page = await self._headless_debug_checkpoint(
                        browser,
                        context,
                        page,
                        p,
                        step_id="after_match_list_populated",
                        note_es="Tras esperar filas de partidos en el listado (puede estar vacio si captcha).",
                    )

                list_miss_loops = 0
                _raw_miss = self._hunter_cfg().get("max_list_dom_miss_loops", 30)
                try:
                    max_list_miss = max(1, int(_raw_miss))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    max_list_miss = 30

                while not self._stop.is_set():
                    if await self._page_needs_auth(page):
                        self._emit("auth_required", {"url": page.url})
                        break

                    found = await self._find_priority_match_row(page)
                    if found is None:
                        loc_chk = await self._match_rows_locator(page)
                        try:
                            nz = await loc_chk.count()
                        except Exception:
                            nz = 0
                        if nz == 0 and self._datadome_captcha_iframe_present(page):
                            self._emit(
                                "error",
                                {
                                    "message": (
                                        "FIFA/DataDome: captcha activo y listado de partidos vacío; "
                                        "headless no puede desbloquear. Resuelva en Chrome visible, "
                                        "guarde session.json y no ejecute hunter en paralelo con esa sesión."
                                    ),
                                    "recoverable": True,
                                },
                            )
                            self._emit_captcha_handoff_required(page, step="match_list")
                            break
                        list_miss_loops += 1
                        if list_miss_loops >= max_list_miss:
                            self._emit(
                                "error",
                                {
                                    "message": (
                                        f"Lista partidos: sin fila que cumpla tras {max_list_miss} recargas "
                                        f"(equipos {self._target_team_ids()}). Revise target_teams o stock FIFA. "
                                        f"URL={page.url[:140]}"
                                    ),
                                    "recoverable": True,
                                },
                            )
                            break
                        if not self._match_list_diagnostic_emitted:
                            self._match_list_diagnostic_emitted = True
                            await self._emit_match_list_dom_diagnostic(page)
                        self._emit("log", {"message": "Sin partido que cumpla criterios (DOM); F5 organico + sync pasivo."})
                        await self._jitter()
                        await self._organic_page_refresh_f5(page)
                        await self._passive_sync_match_list_dom(page, timeout_ms=90_000)
                        continue

                    list_miss_loops = 0
                    row, meta = found

                    try:
                        perf_dbg = meta.get("performance_id") or "?"
                        if not (single_list_pause and bool(self._hunter_cfg().get("headless_step_debug", False))):
                            browser, context, page = await self._headless_debug_checkpoint(
                                browser,
                                context,
                                page,
                                p,
                                step_id="before_open_match",
                                note_es=f"Listado OK; a punto de abrir fila (performance_id={perf_dbg}).",
                            )
                        browser, context, page = await self._pre_seat_browser_validation_gate(
                            browser, context, page, p, meta
                        )
                        await self._open_match_row(page, row, meta)
                        await self._jitter()
                        await self._maybe_click_mejor_sitio(page)
                        await self._jitter()
                        browser, context, page = await self._headless_debug_checkpoint(
                            browser,
                            context,
                            page,
                            p,
                            step_id="after_seat_table_nav",
                            note_es="Tras abrir partido y vista tabla/asientos (antes de elegir categoria).",
                        )

                        pick = await self._pick_category_table_row_and_quantity(page)
                        if pick is None:
                            raise RuntimeError(
                                "Tabla de categorias: ninguna fila cumple categoria, precio y cantidad."
                            )
                        last_hint = pick["price_hint"]
                        qty_chosen = int(pick["quantity"])

                        await self._jitter()
                        await self._humanized_click_book(page)
                        await self._jitter()

                        await context.storage_state(path=str(self.session_path))
                        self._emit(
                            "ticket_secured",
                            {
                                "message": "Boleto Asegurado",
                                "quantity": qty_chosen,
                                "price_hint": last_hint,
                                "performance_id": meta.get("performance_id"),
                                "category": pick.get("category"),
                            },
                        )
                    except SeatFlowBlockedByCaptchaError as exc:
                        self._emit(
                            "log",
                            {
                                "message": (
                                    f"Hunter: captcha en asientos detectado. {exc} "
                                    "Se intentara handoff visible para resolver y continuar en la misma corrida."
                                ),
                            },
                        )
                        self._last_seat_ui_root = None
                        seat_url = page.url
                        try:
                            await context.storage_state(path=str(self.session_path))
                        except Exception:
                            pass
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        self._sync_playwright_session_refs(None, None, None)
                        resumed_ok = await self._captcha_handoff_continue_same_run(p, seat_url, meta)
                        if resumed_ok:
                            break
                        self._emit(
                            "log",
                            {
                                "message": (
                                    "Hunter: handoff visible no concluyo add-to-cart; se detiene "
                                    "esta corrida para evitar recargas ciegas."
                                ),
                            },
                        )
                        break
                    except SeatFlowNaturalEntryError as exc:
                        self._emit(
                            "log",
                            {
                                "message": (
                                    f"Entrada natural no conseguida: {exc} "
                                    "Se vuelve al listado y se reintenta sin forzar /table."
                                ),
                            },
                        )
                        self._last_seat_ui_root = None
                        await self._recover_match_list_after_seat_flow_error(page, list_url)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        self._emit("log", {"message": f"Error en flujo asiento/carrito: {exc}; reintento tras recuperar listado."})
                        self._last_seat_ui_root = None
                        await self._recover_match_list_after_seat_flow_error(page, list_url)
                        continue
                    break

                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
                self._sync_playwright_session_refs(None, None, None)
        except RuntimeError as exc:
            self._emit("error", {"message": str(exc), "recoverable": True})
        except Exception as exc:  # noqa: BLE001
            low = str(exc).lower()
            if "err_tunnel_connection_failed" in low or "tunnel_connection_failed" in low:
                self._emit(
                    "error",
                    {
                        "message": (
                            "Proxy: ERR_TUNNEL_CONNECTION_FAILED (el tunel HTTPS CONNECT al sitio fallo). "
                            "Causas frecuentes: credenciales IPRoyal incorrectas o sesion/lifetime expirado en la "
                            "password; IP bloqueada; VPN que cambia IP respecto a Chrome CDP; proxy que no permite "
                            "HTTPS CONNECT a tickets.fifa.com. Compruebe panel IPRoyal, regenere session en la "
                            "password si aplica, o pruebe server como socks5://host:puerto si su plan lo exige."
                        ),
                        "recoverable": True,
                    },
                )
            else:
                self._emit("error", {"message": str(exc), "recoverable": False})
        finally:
            self._chromium_stealth_plugin = None

        self._emit("log", {"message": "HunterService: fin de run_loop."})
