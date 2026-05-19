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
    if event_type == "pre_seat_browser_validation":
        print("[pre_seat_browser_validation]", flush=True)
        print(f"  list_url={payload.get('list_url')!r}", flush=True)
        print(f"  performance_id={payload.get('performance_id')!r}", flush=True)
        print(f"  timeout_sec={payload.get('timeout_sec')!r}", flush=True)
        print(f"  bot_wall_headless={payload.get('bot_wall_headless')!r}", flush=True)
        print(f"  datadome_iframe_headless={payload.get('datadome_iframe_headless')!r}", flush=True)
        print(f"  chrome_user_data_dir={payload.get('chrome_user_data_dir')!r}", flush=True)
        print("  instructions_es:", flush=True)
        for line in str(payload.get("instructions_es") or "").split("\n"):
            print(f"    {line}", flush=True)
        return
    if event_type == "hunter_checkpoint":
        print("[hunter_checkpoint]", flush=True)
        print(f"  step_id={payload.get('step_id')!r}", flush=True)
        print(f"  bot_wall={payload.get('bot_wall')!r}", flush=True)
        print(f"  datadome_iframe_visible={payload.get('datadome_iframe_visible')!r}", flush=True)
        print(f"  session_snapshot={payload.get('session_snapshot')!r}", flush=True)
        print(f"  pause_sec={payload.get('pause_sec')!r}", flush=True)
        print(f"  wait_for_ui_continue={payload.get('wait_for_ui_continue')!r}", flush=True)
        print(f"  url={payload.get('url')!r}", flush=True)
        print(f"  note={payload.get('note')!r}", flush=True)
        return
    if event_type == "captcha_handoff_required":
        print("[captcha_handoff_required]", flush=True)
        print(f"  step={payload.get('step')!r}", flush=True)
        print(f"  handoff_url={payload.get('handoff_url')!r}", flush=True)
        print(f"  performance_id={payload.get('performance_id')!r}", flush=True)
        print(f"  target_teams={payload.get('target_teams')!r}", flush=True)
        print("  instructions_es:", flush=True)
        for line in str(payload.get("instructions_es") or "").split("\n"):
            print(f"    {line}", flush=True)
        return
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
