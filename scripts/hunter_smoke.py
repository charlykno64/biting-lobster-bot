#!/usr/bin/env python3
"""
Humo del Hunter: lee config.yaml, muestra perfil de jitter y (por defecto) lanza
Chromium headless + stealth con session.json y abre la URL de lista de partidos
(hunter.match_list_url, wait_until=domcontentloaded).

Uso (desde la raíz del repo):
  .venv\\Scripts\\python.exe scripts\\hunter_smoke.py
  .venv\\Scripts\\python.exe scripts\\hunter_smoke.py --skip-browser
  .venv\\Scripts\\python.exe scripts\\hunter_smoke.py --jitter-samples 5
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from core.HunterService import HunterService
from core.hunter_prereqs import validate_hunter_search_objective
from data.ConfigRepository import ConfigRepository


def _print_jitter_samples(profile: dict[str, object], n: int) -> None:
    lo = float(profile["min_sec"])
    hi = float(profile["max_sec"])
    print(f"Muestras de jitter uniforme ({n}):", flush=True)
    for i in range(n):
        ms = random.uniform(lo, hi) * 1000.0
        print(f"  [{i + 1}] {ms:.1f} ms", flush=True)


async def _browser_smoke(session_path: Path, list_url: str, initial_delay_sec: float) -> None:
    print(
        "AVISO: cierra Chrome de captura CDP antes del humo; no uses la misma URL en dos navegadores a la vez.",
        flush=True,
    )
    print("Navegador: Stealth + Chromium headless + storage_state...", flush=True)
    print(f"URL lista partidos (domcontentloaded): {list_url[:120]}...", flush=True)
    if initial_delay_sec > 0:
        print(f"Pausa inicial {initial_delay_sec}s...", flush=True)
        await asyncio.sleep(initial_delay_sec)
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=str(session_path),
            viewport={"width": 1360, "height": 900},
            locale="es-MX",
        )
        page = await context.new_page()
        body_lo = ""
        try:
            await page.goto(list_url, wait_until="domcontentloaded", timeout=90_000)
            final_url = page.url
            title = await page.title()
            body_lo = (await page.locator("body").inner_text(timeout=8_000)).lower()
        finally:
            await browser.close()
    print(f"OK: URL final = {final_url}", flush=True)
    print(f"OK: titulo = {title!r}", flush=True)
    if any(
        x in body_lo
        for x in (
            "este bloqueo",
            "sobrehumana",
            "un robot",
            "misma red",
            "dificultades para acceder",
            "restringido temporalmente",
            "acceso está restringido",
            "acceso esta restringido",
        )
    ):
        print(
            "ADVERTENCIA: el HTML parece la pantalla anti-bot de FIFA. Cierra Chrome de captura y no "
            "mezcles la misma URL en dos navegadores; sube hunter.initial_delay_sec y espera unos minutos.",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Humo Hunter: config + jitter + navegador opcional.")
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Solo config y jitter; no lanza Playwright.",
    )
    parser.add_argument(
        "--jitter-samples",
        type=int,
        default=0,
        metavar="N",
        help="Imprime N muestras aleatorias de retraso (ms) según el rango actual.",
    )
    args = parser.parse_args()

    config_path = ROOT / "config.yaml"
    repo = ConfigRepository(str(config_path))
    config = repo.load()
    hunter = HunterService(ROOT, config)
    profile = hunter.jitter_profile()

    print(f"project_root = {ROOT}", flush=True)
    print(f"config.yaml existe = {config_path.is_file()}", flush=True)
    print(f"hunter.speed (normalizado) = {profile['speed']}", flush=True)
    print(
        f"jitter entre acciones (aprox.): {profile['min_ms']}-{profile['max_ms']} ms "
        f"({profile['min_sec']}-{profile['max_sec']} s)",
        flush=True,
    )

    if args.jitter_samples > 0:
        _print_jitter_samples(profile, args.jitter_samples)

    session_path = ROOT / "session.json"
    if args.skip_browser:
        print("--skip-browser: no se abre Chromium.", flush=True)
        return

    if not session_path.is_file():
        print(f"ERROR: falta {session_path} (captura sesion con Epic 1 primero).", flush=True)
        sys.exit(1)

    ok, msg = validate_hunter_search_objective(config)
    if not ok:
        print(f"ERROR: {msg}", flush=True)
        sys.exit(1)

    init = float((config.get("hunter") or {}).get("initial_delay_sec", 3.5))
    asyncio.run(_browser_smoke(session_path, hunter.match_list_url(), init))


if __name__ == "__main__":
    main()
