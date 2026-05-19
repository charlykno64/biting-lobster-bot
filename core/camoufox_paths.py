"""Resolución de ruta al ejecutable Camoufox / Firefox para hunter y captura de sesión."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def find_camoufox_exe(path_or_dir: Path) -> Path | None:
    """path_or_dir puede ser el .exe o la carpeta del build (p. ej. .../official/135.x-beta.y)."""
    try:
        p = path_or_dir.expanduser()
        if not p.is_absolute():
            p = p.resolve()
    except (OSError, RuntimeError):
        p = path_or_dir
    try:
        if p.is_file() and p.suffix.lower() == ".exe":
            return p
    except OSError:
        return None
    if not p.is_dir():
        return None
    for name in ("camoufox.exe", "Firefox.exe", "firefox.exe"):
        cand = p / name
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    try:
        for sub in p.iterdir():
            if not sub.is_dir():
                continue
            for name in ("camoufox.exe", "Firefox.exe", "firefox.exe"):
                cand = sub / name
                try:
                    if cand.is_file():
                        return cand
                except OSError:
                    continue
    except OSError:
        pass
    try:
        for cand in p.rglob("camoufox.exe"):
            if cand.is_file():
                return cand
    except OSError:
        pass
    return None


def resolve_camoufox_executable(hunter_cfg: dict[str, Any] | None) -> Path | None:
    """
    Orden: hunter.camoufox_executable, CAMOUFOX_PATH, heurísticas bajo %LOCALAPPDATA%.
    Acepta ruta al .exe o carpeta del navegador.
    """
    h = hunter_cfg or {}
    candidates: list[Path] = []
    raw = h.get("camoufox_executable")
    if isinstance(raw, str) and raw.strip():
        expanded = os.path.expandvars(raw.strip().strip('"').strip("'"))
        candidates.append(Path(expanded))
    env = os.environ.get("CAMOUFOX_PATH", "").strip()
    if env:
        candidates.append(Path(os.path.expandvars(env)))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = Path(local) / "camoufox" / "camoufox" / "Cache" / "browsers" / "official"
        try:
            if base.is_dir():
                vers = sorted(
                    [d for d in base.iterdir() if d.is_dir()],
                    key=lambda x: x.name,
                    reverse=True,
                )
                for d in vers[:3]:
                    candidates.append(d)
        except OSError:
            pass
        candidates.extend(
            [
                Path(local) / "Camoufox" / "Camoufox.exe",
                Path(local) / "camoufox" / "Camoufox.exe",
            ]
        )
    seen: set[str] = set()
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        found = find_camoufox_exe(c)
        if found is not None:
            return found
    return None
