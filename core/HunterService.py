from __future__ import annotations

import asyncio
import inspect
import random
import re
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Page, Response, async_playwright
from playwright_stealth import Stealth

from core.currency import CurrencyConverter

TICKETS_HOME_URL = "https://fwc26-shop-mex.tickets.fifa.com/secured/content"
FIFA_HOST = "tickets.fifa.com"
# Tras COMPRAR BOLETOS: /secure/... o /secured/... + selection/event/date (a veces con /product/<id>).
_DATE_SELECTION_RE = re.compile(
    r"https?://[^/]*tickets\.fifa\.com/(?:secure|secured)/selection/event/date",
    re.I,
)

# Jitter entre pasos: uniforme dentro del rango (segundos), según `hunter.speed` en config.yaml.
_SPEED_BOUNDS_SEC: dict[str, tuple[float, float]] = {
    "alta": (0.200, 0.399),
    "media": (0.400, 0.799),
    "baja": (0.800, 1.200),
}
_SPEED_ALIASES: dict[str, str] = {"high": "alta", "medium": "media", "low": "baja"}

EventCallback = Callable[[str, dict[str, Any]], Any]


class HunterService:
    """
    Motor de cacería asíncrono: **no usa CDP**. Tras capturar la sesión con Chrome+CDP (Epic 1) y
    `session.json`, aquí se arranca **Chromium nuevo** con stealth + headless y `storage_state`.
    Conviene que el usuario **cierre Chrome** usado para la captura antes de iniciar el hunter.

    Jitter configurable: `hunter.speed` = alta | media | baja (default baja).
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
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._availability_event = asyncio.Event()
        self._last_price_hint: dict[str, Any] | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def _criteria(self) -> dict[str, Any]:
        return self.config.get("search_criteria", {}) or {}

    def _normalized_speed_key(self) -> str:
        raw = (self.config.get("hunter") or {}).get("speed", "baja")
        if not isinstance(raw, str):
            return "baja"
        key = raw.strip().lower()
        key = _SPEED_ALIASES.get(key, key)
        return key if key in _SPEED_BOUNDS_SEC else "baja"

    def _jitter_bounds_sec(self) -> tuple[float, float]:
        return _SPEED_BOUNDS_SEC[self._normalized_speed_key()]

    def jitter_profile(self) -> dict[str, Any]:
        """Resumen de `hunter.speed` para diagnóstico o scripts de humo."""
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
        except Exception as exc:  # noqa: BLE001 — no tumbar el hunter por UI
            _ = exc

    async def _jitter(self) -> None:
        lo, hi = self._jitter_bounds_sec()
        if hi < lo:
            lo, hi = hi, lo
        await asyncio.sleep(random.uniform(lo, hi))

    def _schedule_response(self, response: Response) -> None:
        task = asyncio.create_task(self._handle_response(response))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _handle_response(self, response: Response) -> None:
        if self._stop.is_set():
            return
        if FIFA_HOST not in response.url:
            return
        if response.status == 403:
            self._emit("error", {"message": "HTTP 403 en respuesta FIFA", "recoverable": True})
            return
        rt = response.request.resource_type
        if rt not in ("xhr", "fetch"):
            return
        ct = (response.headers or {}).get("content-type", "")
        if "json" not in ct.lower():
            return
        try:
            data = await response.json()
        except Exception:
            return
        if self._json_suggests_availability(data):
            hint = self._extract_price_hint(data)
            if hint:
                self._last_price_hint = hint
            self._availability_event.set()
            self._emit(
                "availability",
                {"url": response.url, "hint_keys": list(hint.keys()) if hint else []},
            )

    def _json_suggests_availability(self, obj: Any) -> bool:
        text_blob = self._json_blob_lower(obj)
        if not text_blob:
            return False
        blocked = ("no disponible", "sold out", "agotado", "unavailable")
        if any(b in text_blob for b in blocked):
            return False
        positive = (
            "available",
            "disponible",
            "inventory",
            "remaining",
            "quantity",
            "stock",
            "seats",
            "asientos",
        )
        return any(p in text_blob for p in positive)

    def _json_blob_lower(self, obj: Any, depth: int = 0) -> str:
        if depth > 12:
            return ""
        if isinstance(obj, dict):
            parts: list[str] = []
            for k, v in obj.items():
                parts.append(str(k).lower())
                parts.append(self._json_blob_lower(v, depth + 1))
            return " ".join(parts)
        if isinstance(obj, list):
            return " ".join(self._json_blob_lower(x, depth + 1) for x in obj[:80])
        if isinstance(obj, str):
            return obj.lower()
        return ""

    def _extract_price_hint(self, obj: Any) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        conv = CurrencyConverter(self.config)

        def walk(node: Any, depth: int) -> None:
            nonlocal best
            if depth > 14:
                return
            if isinstance(node, dict):
                keys = {str(k).lower(): k for k in node}
                lk = set(keys)
                amount = None
                cur = None
                for cand in ("totalprice", "price", "amount", "minprice", "maxprice"):
                    if cand in lk:
                        val = node[keys[cand]]
                        if isinstance(val, (int, float)):
                            amount = float(val)
                        elif isinstance(val, str):
                            try:
                                amount, cur = conv.parse_price_text(val)
                            except Exception:
                                pass
                for cand in ("currency", "currencycode", "currency_iso", "currencycodeiso"):
                    if cand in lk and isinstance(node[keys[cand]], str):
                        cur = str(node[keys[cand]])
                if amount is not None:
                    best = {"amount": amount, "currency": (cur or "USD").upper()}
                for v in node.values():
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for item in node[:120]:
                    walk(item, depth + 1)

        walk(obj, 0)
        return best

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

    async def _click_comprar_boletos(self, page: Page) -> None:
        """
        En /secured/content la card de producto expone el mismo destino por:
        - enlace principal (stx-MainActionArea + productId),
        - botón COMPRAR BOLETOS (g-Button-primary + productId),
        - cualquier <a> con productId dentro de stx-ProductCard.
        """
        timeout = 60_000
        strategies: list[tuple[str, Any]] = [
            (
                "stx-MainActionArea+productId",
                page.locator(
                    'a.stx-MainActionArea[href*="/secured/selection/event/date"][href*="productId="]'
                ),
            ),
            (
                "g-Button-primary+productId",
                page.locator(
                    'a.g-Button-primary[href*="/secured/selection/event/date"][href*="productId="]'
                ),
            ),
            (
                "stx-ProductCard a+productId",
                page.locator(
                    'div[class*="stx-ProductCard"] a[href*="/secured/selection/event/date"][href*="productId="]'
                ),
            ),
        ]
        for label, loc in strategies:
            try:
                n = await loc.count()
                if n == 0:
                    continue
                await loc.first.click(timeout=timeout)
                self._emit("log", {"message": f"COMPRAR BOLETOS: clic con estrategia {label!r} (coincidencias={n})."})
                return
            except Exception:
                continue

        link = page.get_by_role("link", name=re.compile(r"comprar\s+boletos", re.I))
        if await link.count() > 0:
            await link.first.click(timeout=timeout)
            self._emit("log", {"message": "COMPRAR BOLETOS: clic por aria-label (fallback)."})
            return
        alt = page.locator("a").filter(has_text=re.compile(r"COMPRAR\s+BOLETOS", re.I))
        await alt.first.click(timeout=timeout)
        self._emit("log", {"message": "COMPRAR BOLETOS: clic por texto visible (fallback)."})

    async def _wait_date_selection_screen(self, page: Page) -> None:
        await page.wait_for_url(_DATE_SELECTION_RE, timeout=90_000)
        self._emit("log", {"message": f"Pantalla fecha/equipo: {page.url[:140]}..."})

    async def _select_team(self, page: Page) -> None:
        teams = self._criteria().get("target_teams") or []
        if not teams:
            raise RuntimeError("search_criteria.target_teams vacío en config.")
        team_id = str(teams[0])
        sel = page.locator("#team")
        await sel.wait_for(state="attached", timeout=60_000)
        await sel.scroll_into_view_if_needed()
        try:
            await sel.wait_for(state="visible", timeout=8_000)
        except Exception:
            pass
        await sel.select_option(value=team_id, timeout=30_000)
        self._emit(
            "log",
            {
                "message": (
                    f"Equipo seleccionado (option value={team_id}); "
                    "listado puede actualizarse por XHR sin recarga completa."
                ),
            },
        )

    async def _open_first_actionable_match(self, page: Page) -> bool:
        links = page.locator('a[href*="/selection/event"]')
        n = await links.count()
        for i in range(n):
            if self._stop.is_set():
                return False
            link = links.nth(i)
            try:
                container = link.locator(
                    "xpath=ancestor::*[self::article or self::tr or self::li or self::div][1]"
                )
                blob = (await container.inner_text()).lower() if await container.count() else ""
            except Exception:
                blob = ""
            if "no disponible" in blob:
                continue
            await link.click(timeout=45_000)
            return True
        return False

    async def _click_reservar_mejor_sitio(self, page: Page) -> None:
        btn = page.get_by_text(re.compile(r"reservar\s+el\s+mejor\s+sitio", re.I))
        await btn.first.click(timeout=60_000)

    async def _pick_category_by_priority(self, page: Page) -> None:
        preferred = self._criteria().get("preferred_categories") or [1, 2, 3, 4]
        for cat in preferred:
            if self._stop.is_set():
                return
            pattern = re.compile(rf"categor[ií]a\s*{cat}\b", re.I)
            loc = page.locator("a, button, [role='button'], label, div, span").filter(has_text=pattern)
            if await loc.count() == 0:
                continue
            first = loc.first
            try:
                if await first.is_enabled():
                    await first.click(timeout=20_000)
                    self._emit("log", {"message": f"Categoría priorizada clicada: {cat}"})
                    return
            except Exception:
                continue
        self._emit("log", {"message": "No se encontró categoría clickeable; se intenta flujo por defecto."})

    async def _set_quantity_one(self, page: Page) -> None:
        qty = page.locator("select[name*='quantity' i], select[id*='quantity' i]").first
        if await qty.count():
            try:
                await qty.select_option("1")
            except Exception:
                pass

    async def _humanized_click_book(self, page: Page) -> None:
        book = page.locator("#book")
        await book.wait_for(state="visible", timeout=90_000)
        box = await book.bounding_box()
        if not box:
            await book.click(timeout=30_000)
            return
        pad_x = min(12.0, max(4.0, box["width"] * 0.15))
        pad_y = min(10.0, max(3.0, box["height"] * 0.2))
        x = box["x"] + random.uniform(pad_x, max(pad_x + 1.0, box["width"] - pad_x))
        y = box["y"] + random.uniform(pad_y, max(pad_y + 1.0, box["height"] - pad_y))
        await page.mouse.click(x, y)

    async def _page_needs_auth(self, page: Page) -> bool:
        u = page.url.lower()
        return "login" in u and FIFA_HOST in u

    async def run_loop(self) -> None:
        """Bucle principal hasta stop, auth requerida o evento ticket_secured."""
        if not self.session_path.is_file():
            self._emit("error", {"message": f"No existe {self.session_path}", "recoverable": False})
            return

        self._stop.clear()
        self._availability_event.clear()
        self._last_price_hint = None

        self._emit(
            "log",
            {
                "message": (
                    "HunterService: Chromium headless + stealth, storage_state=session.json (sin CDP). "
                    f"Velocidad/jitter: {self._normalized_speed_key()}. "
                    "Cierra Google Chrome de la captura si sigue abierto para liberar recursos."
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
                page.on("response", self._schedule_response)

                await page.goto(TICKETS_HOME_URL, wait_until="domcontentloaded", timeout=90_000)
                await self._jitter()
                if await self._page_needs_auth(page):
                    self._emit("auth_required", {"url": page.url})
                    await browser.close()
                    return

                await self._click_comprar_boletos(page)
                await self._wait_date_selection_screen(page)
                await self._jitter()
                await self._select_team(page)
                await self._jitter()

                while not self._stop.is_set():
                    if await self._page_needs_auth(page):
                        self._emit("auth_required", {"url": page.url})
                        break

                    try:
                        await asyncio.wait_for(self._availability_event.wait(), timeout=45.0)
                    except asyncio.TimeoutError:
                        self._emit("log", {"message": "Sin señal XHR reciente; recargando listado."})
                        await page.reload(wait_until="domcontentloaded")
                        await self._jitter()
                        continue

                    self._availability_event.clear()
                    if not self._price_within_budget(self._last_price_hint):
                        self._emit(
                            "log",
                            {"message": "Disponibilidad detectada pero fuera de max_price_cents; se continúa."},
                        )
                        await self._jitter()
                        continue

                    opened = await self._open_first_actionable_match(page)
                    if not opened:
                        await self._jitter()
                        continue

                    await self._jitter()
                    await self._click_reservar_mejor_sitio(page)
                    await self._jitter()
                    await self._pick_category_by_priority(page)
                    await self._set_quantity_one(page)
                    await self._jitter()

                    qty_target = max(1, int(self._criteria().get("quantity", 1)))
                    for _ in range(qty_target):
                        if self._stop.is_set():
                            break
                        await self._humanized_click_book(page)
                        await self._jitter()

                    await context.storage_state(path=str(self.session_path))
                    self._emit(
                        "ticket_secured",
                        {
                            "message": "Boleto Asegurado",
                            "quantity": qty_target,
                            "price_hint": self._last_price_hint,
                        },
                    )
                    break

                await browser.close()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"message": str(exc), "recoverable": False})

        self._emit("log", {"message": "HunterService: fin de run_loop."})
