#!/usr/bin/env python3
"""
Ejecuta el bucle completo del HunterService (camino feliz hasta carrito o error).

Requisitos en la raíz del repo: session.json, config.yaml con search_criteria y hunter.

Uso (PowerShell, desde la raíz del proyecto):
  .\\.venv\\Scripts\\python.exe scripts\\run_hunter.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.HunterService import HunterService
from core.hunter_prereqs import validate_hunter_search_objective
from data.ConfigRepository import ConfigRepository


def _print_event(event_type: str, payload: dict[str, Any]) -> None:
    print(f"[{event_type}] {payload}", flush=True)


def main() -> None:
    print(
        "AVISO FIFA: cierra Google Chrome usado para la captura CDP antes de ejecutar el hunter. "
        "No abras en Chrome la misma URL mientras Playwright corre (misma sesion / IP puede disparar "
        "el bloqueo anti-bot).",
        flush=True,
    )
    session = ROOT / "session.json"
    if not session.is_file():
        print(f"ERROR: falta {session} (captura sesión desde la app o SessionManager).", flush=True)
        sys.exit(1)
    config_path = ROOT / "config.yaml"
    if not config_path.is_file():
        print(f"ERROR: falta {config_path}.", flush=True)
        sys.exit(1)

    config = ConfigRepository(str(config_path)).load()
    ok, msg = validate_hunter_search_objective(config)
    if not ok:
        print(f"ERROR: {msg}", flush=True)
        sys.exit(1)
    hunter = HunterService(ROOT, config, on_event=_print_event)
    asyncio.run(hunter.run_loop())


if __name__ == "__main__":
    main()
