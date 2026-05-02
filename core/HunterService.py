from __future__ import annotations

import asyncio
import inspect
import random
import re
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Locator, Page, async_playwright
from playwright_stealth import Stealth

from core.currency import CurrencyConverter
from core.hunter_prereqs import validate_hunter_search_objective

FIFA_HOST = "tickets.fifa.com"
DEFAULT_SHOP_HOST = "https://fwc26-shop-mex.tickets.fifa.com"
DEFAULT_PRODUCT_ID = "10229225515651"
TICKETS_HOME_URL = "https://fwc26-shop-mex.tickets.fifa.com/secured/content"
_DATE_SELECTION_RE = re.compile(
    r"https?://[^/]*tickets\.fifa\.com/(?:secure|secured)/selection/event/date",
    re.I,
)

# Jitter (segundos), según hunter.speed en config.yaml.
_SPEED_BOUNDS_SEC: dict[str, tuple[float, float]] = {
    "alta": (0.200, 0.399),
    "media": (0.400, 0.799),
    "baja": (0.800, 1.200),
}
_SPEED_ALIASES: dict[str, str] = {"high": "alta", "medium": "media", "low": "baja"}

EventCallback = Callable[[str, dict[str, Any]], Any]


class HunterService:
    """
    Cacería sin CDP: Chromium headless + stealth + session.json.

    Entrada **natural** (por defecto): `/secured/content` → clic COMPRAR BOLETOS →
    pantalla de fechas; luego, si la URL no es aún la canónica del listado SSR,
    `goto(match_list_url())`. Así el flujo se parece al usuario y el DOM queda
    alineado con la URL `/secure/.../date/product/<id>/lang/<lang>`.

    Con `hunter.skip_secured_content: true` se salta la tienda y se abre solo
    `match_list_url()` (útil para pruebas).

    Tras elegir partido, por defecto se hace `goto` a
    `.../seat/performance/<perfId>/table/<seat_table_index>/lang/...` para evitar
    el mapa lento; con `hunter.use_seat_map_entry: true` se usa el clic del listado
    (flujo con mapa + botón Mejor sitio).

    Solo `wait_until=\"domcontentloaded\"` en goto/reload — nunca networkidle.
    """

    def __init__(
        self,
        project_root: Path,
        config: dict[str, Any],
        *,
        session_file: str = "session.json",
        on_event: EventCallback | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.config = config
        self.session_path = self.project_root / session_file
        self._on_event = on_event
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _hunter_cfg(self) -> dict[str, Any]:
        return self.config.get("hunter") or {}

    def _criteria(self) -> dict[str, Any]:
        return self.config.get("search_criteria", {}) or {}

    def match_list_url(self) -> str:
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        product_id = str(h.get("product_id", DEFAULT_PRODUCT_ID))
        lang = str(h.get("lang", "es"))
        return f"{host}/secure/selection/event/date/product/{product_id}/lang/{lang}"

    def _canonical_list_path_marker(self) -> str:
        pid = str(self._hunter_cfg().get("product_id", DEFAULT_PRODUCT_ID))
        return f"/product/{pid}/"

    def _seat_table_url(self, performance_id: str, table_index: int | None = None) -> str:
        h = self._hunter_cfg()
        host = str(h.get("shop_host", DEFAULT_SHOP_HOST)).rstrip("/")
        lang = str(h.get("lang", "es"))
        idx = table_index if table_index is not None else int(h.get("seat_table_index", 1))
        return (
            f"{host}/secure/selection/event/seat/performance/{performance_id}"
            f"/table/{idx}/lang/{lang}"
        )

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
        await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)

    async def _enter_match_list_page(self, page: Page) -> str:
        """
        Devuelve la URL canónica del listado usada para el bucle (match_list_url).
        """
        list_url = self.match_list_url()
        skip_home = bool(self._hunter_cfg().get("skip_secured_content", False))
        if skip_home:
            await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
            self._emit("log", {"message": f"Entrada directa al listado (skip_secured_content): {list_url[:100]}..."})
            return list_url

        await page.goto(TICKETS_HOME_URL, wait_until="domcontentloaded", timeout=90_000)
        self._emit("log", {"message": f"Tienda inicial: {TICKETS_HOME_URL}"})
        await self._click_comprar_boletos(page)
        await page.wait_for_url(_DATE_SELECTION_RE, timeout=90_000)
        self._emit("log", {"message": f"Tras COMPRAR BOLETOS: {page.url[:140]}..."})

        if self._canonical_list_path_marker() not in page.url:
            await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
            self._emit("log", {"message": "Normalizado a URL canónica del listado SSR (product/lang)."})

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

    def _hint_from_amount_attrs(self, raw: str | None, cls: str) -> dict[str, Any] | None:
        if not raw:
            return None
        currency = "USD"
        for code in ("MXN", "USD", "CAD"):
            if f"amount_{code}" in cls:
                currency = code
                break
        try:
            minor = int(str(raw).strip())
        except ValueError:
            return None
        amount_major = minor / 1000.0
        return {"amount": amount_major, "currency": currency}

    async def _price_from_amount_span(self, span: Locator) -> dict[str, Any] | None:
        if await span.count() == 0:
            return None
        raw = await span.get_attribute("data-amount")
        cls = await span.get_attribute("class") or ""
        return self._hint_from_amount_attrs(raw, cls)

    @staticmethod
    def _row_availability_class(class_attr: str) -> str | None:
        if not class_attr:
            return None
        if "sold_out" in class_attr:
            return "sold_out"
        parts = class_attr.split()
        if "available" in parts:
            return "available"
        return None

    async def _price_from_row(self, row: Locator) -> dict[str, Any] | None:
        return await self._price_from_amount_span(row.locator("span.amount[data-amount]").first)

    @staticmethod
    def _category_number_from_th_text(text: str) -> int | None:
        m = re.search(r"categor[ií]a\s*(\d)", text, re.I)
        if m:
            return int(m.group(1))
        return None

    async def _row_has_hospitality_cta(self, row: Locator) -> bool:
        loc = row.get_by_text(re.compile(r"hospitality", re.I))
        return await loc.count() > 0

    async def _find_priority_match_row(self, page: Page) -> tuple[Locator, dict[str, Any]] | None:
        """Primera fila li.performance: equipos (orden target_teams) y disponibilidad; precio se valida en tabla de categorías."""
        target_ids = self._target_team_ids()
        if not target_ids:
            raise RuntimeError("search_criteria.target_teams vacío en config.")

        rows = page.locator("li.performance")
        n = await rows.count()
        for priority_tid in target_ids:
            for i in range(n):
                if self._stop.is_set():
                    return None
                row = rows.nth(i)
                cls = await row.get_attribute("class") or ""
                if self._row_availability_class(cls) != "available":
                    continue
                host = await row.get_attribute("data-host-team-id")
                guest = await row.get_attribute("data-opposing-team-id")
                ids = {host, guest}
                if priority_tid not in ids:
                    continue
                if await self._row_has_hospitality_cta(row):
                    continue
                perf_id = await row.get_attribute("id")
                return row, {
                    "target_team_id": priority_tid,
                    "host_team_id": host,
                    "opposing_team_id": guest,
                    "performance_id": perf_id,
                }
        return None

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
            await page.wait_for_url(
                re.compile(r"/(?:secure|secured)/selection/event/seat", re.I),
                timeout=90_000,
            )
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

    async def _pick_category_table_row_and_quantity(self, page: Page) -> dict[str, Any] | None:
        """
        Tabla de categorías: primer th = categoría; overlay category_unavailable_overlay = sin stock;
        primer td = select eventFormData[n].quantity; segundo td = precio (data-amount).
        Prioridad: preferred_categories. Cantidad: máximo valor de option <= quantity en config.
        """
        preferred = self._criteria().get("preferred_categories") or [1, 2, 3, 4]
        qty_limit = max(1, int(self._criteria().get("quantity", 1)))
        table_rows = page.locator("table tr")
        n = await table_rows.count()

        for cat_priority in preferred:
            if self._stop.is_set():
                return None
            for i in range(n):
                row = table_rows.nth(i)
                th0 = row.locator("th").first
                if await th0.count() == 0:
                    continue
                if await th0.locator("div.category_unavailable_overlay").count() > 0:
                    continue
                th_text = await th0.inner_text()
                cn = self._category_number_from_th_text(th_text)
                if cn != cat_priority:
                    continue

                tds = row.locator("td")
                if await tds.count() < 2:
                    continue
                sel = tds.nth(0).locator('select[id*="quantity"]')
                if await sel.count() == 0:
                    continue

                price_span = tds.nth(1).locator("span.amount[data-amount]").first
                hint = await self._price_from_amount_span(price_span)
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

        return None

    async def _humanized_click_book(self, page: Page) -> None:
        primary = page.locator("a#book")
        if await primary.count() > 0:
            target = primary.first
        else:
            candidates = page.locator('a[id^="book"]')
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
                target = page.locator("#book").first
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
        )
        return any(m in body for m in markers)

    async def run_loop(self) -> None:
        if not self.session_path.is_file():
            self._emit("error", {"message": f"No existe {self.session_path}", "recoverable": False})
            return

        ok, prereq_msg = validate_hunter_search_objective(self.config)
        if not ok:
            self._emit("error", {"message": prereq_msg, "recoverable": False})
            return

        self._stop.clear()

        self._emit(
            "log",
            {
                "message": (
                    "HunterService: headless + stealth + session.json; "
                    f"domcontentloaded only; jitter={self._normalized_speed_key()}."
                ),
            },
        )

        try:
            async with Stealth().use_async(async_playwright()) as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    storage_state=str(self.session_path),
                    viewport={"width": 1360, "height": 900},
                    locale="es-MX",
                )
                page = await context.new_page()

                init_delay = float(self._hunter_cfg().get("initial_delay_sec", 3.5))
                if init_delay > 0:
                    self._emit(
                        "log",
                        {
                            "message": (
                                f"Pausa inicial {init_delay}s antes de tocar FIFA "
                                "(reduce patron 'sobrehumano'; cierra Chrome de captura si sigue abierto)."
                            ),
                        },
                    )
                    await asyncio.sleep(init_delay)

                list_url = await self._enter_match_list_page(page)
                if await self._page_needs_auth(page):
                    self._emit("auth_required", {"url": page.url})
                    await browser.close()
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
                    await browser.close()
                    return

                while not self._stop.is_set():
                    if await self._page_needs_auth(page):
                        self._emit("auth_required", {"url": page.url})
                        break

                    found = await self._find_priority_match_row(page)
                    if found is None:
                        self._emit("log", {"message": "Sin partido que cumpla criterios (DOM); recarga tras jitter."})
                        await self._jitter()
                        await page.reload(wait_until="domcontentloaded", timeout=90_000)
                        continue

                    row, meta = found

                    try:
                        await self._open_match_row(page, row, meta)
                        await self._jitter()
                        await self._maybe_click_mejor_sitio(page)
                        await self._jitter()

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
                    except Exception as exc:  # noqa: BLE001
                        self._emit("log", {"message": f"Error en flujo asiento/carrito: {exc}; reintento tras recarga."})
                        await self._jitter()
                        await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
                        continue
                    break

                await browser.close()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"message": str(exc), "recoverable": False})

        self._emit("log", {"message": "HunterService: fin de run_loop."})
