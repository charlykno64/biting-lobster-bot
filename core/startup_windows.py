from __future__ import annotations

import os
from pathlib import Path


def _startup_bat_path() -> Path:
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup_dir / "BitingLobsterStart.bat"


def set_start_on_boot(enabled: bool, project_root: Path) -> None:
    bat_file = _startup_bat_path()
    if enabled:
        command = f'@echo off\ncd /d "{project_root}"\nstart "" "{project_root / ".venv" / "Scripts" / "python.exe"}" "{project_root / "ui" / "app.py"}"\n'
        bat_file.write_text(command, encoding="utf-8")
    elif bat_file.exists():
        bat_file.unlink()
