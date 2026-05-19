from __future__ import annotations

import asyncio
import shutil
import sys
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

import flet as ft
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.currency import CurrencyConverter
from core.HunterService import DEFAULT_PRODUCT_ID, HunterService
from core.geo import check_geolocation_allowed
from core.hardware import get_hardware_id
from core.startup_windows import set_start_on_boot
from data.ConfigRepository import ConfigRepository
from data.LicenseRepository import LicenseRepository
from data.SessionManager import (
    CDP_ENDPOINT,
    SessionManager,
    TICKETS_HOME_URL,
    onboarding_cdp_startup_hygiene,
    open_url_in_cdp_new_tab,
    wait_cdp_http_ready,
)
from core.playwright_proxy import chrome_cdp_manual_proxy_auth_hint, chrome_extra_args_from_hunter_cfg
from core.hunter_prereqs import validate_hunter_search_objective
from data.chrome_cdp_queue_probe import detect_queue_restriction_via_cdp


TEAM_OPTIONS = [
    ("10229225507168", "Mexico"),
    ("10229225507169", "United States"),
    ("10229225507167", "Canada"),
    ("11404606516", "Argentina"),
    ("11404606535", "Brasil"),
    ("11404606677", "Espana"),
    ("11404606577", "Francia"),
    ("11404606568", "Inglaterra"),
    ("11404606634", "Marruecos"),
    ("11404606654", "Portugal"),
    ("11404606582", "Alemania"),
    ("11404606664", "Arabia Saudi"),
    ("11404606519", "Australia"),
    ("11404606520", "Austria"),
    ("11404606527", "Belgica"),
    ("11404606550", "Colombia"),
    ("11404606556", "Croacia"),
    ("11404606565", "Ecuador"),
    ("11404606566", "Egipto"),
    ("11404606665", "Escocia"),
    ("11404606583", "Ghana"),
    ("11404606592", "Haiti"),
    ("11404606604", "Japon"),
    ("11404606605", "Jordania"),
    ("11404606646", "Noruega"),
    ("11404606650", "Panama"),
    ("11404606652", "Paraguay"),
    ("11404606639", "Paises Bajos"),
    ("11404606685", "Suiza"),
    ("11404606702", "Uruguay"),
]

CATEGORY_OPTIONS = [1, 2, 3, 4]
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
# Pagina informativa en fifa.com; CDP: about:blank + higiene salvo hunter.attach_hunter_to_chrome_cdp (tienda directa).
FIFA_COM_TICKETS_PAGE = "https://www.fifa.com/es/tournaments/mens/worldcup/canadamexicousa2026/tickets"
DEFAULT_CHROME_USER_DATA = Path(r"C:\BitingLobsterChromeProfile")


def _chrome_profile_dir_from_cfg(cfg: dict[str, Any]) -> Path:
    app = cfg.get("app") or {}
    raw = app.get("biting_lobster_chrome_profile")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip().strip('"').strip("'"))
    return DEFAULT_CHROME_USER_DATA


def _chrome_runs_dir_from_cfg(cfg: dict[str, Any]) -> Path:
    app = cfg.get("app") or {}
    raw = app.get("chrome_profile_runs_root")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip().strip('"').strip("'"))
    return Path(str(_chrome_profile_dir_from_cfg(cfg)) + "_runs")


def _kill_windows_chrome_cdp_port_9222() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
        "| Where-Object { $_.CommandLine -match 'remote-debugging-port=9222' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )


class DashboardApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.project_root = PROJECT_ROOT
        load_dotenv(self.project_root / ".env.dev")

        self.config_repo = ConfigRepository(str(self.project_root / "config.yaml"))
        self.license_repo = LicenseRepository()
        self.hardware_id = get_hardware_id()
        self.config = self.config_repo.load()

        self.log_console = ft.TextField(
            label="LogConsole",
            multiline=True,
            min_lines=10,
            max_lines=18,
            read_only=True,
            value="",
            width=520,
            height=280,
        )
        self.status_text = ft.Text("Estado: inicializando...")
        self.supabase_status_text = ft.Text("Supabase: verificando...")
        self.last_sync_text = ft.Text("Ultima sincronizacion: --:--:--", size=12, color=ft.Colors.GREY_400)
        self.polling_active = False
        self._chrome_onboarding_last_launch = 0.0
        self._onboarding_poll_cancel: asyncio.Event | None = None
        self._hunter_running = False
        self._hunter_button: ft.Button | None = None
        self._hunter_debug_continue: asyncio.Event | None = None
        self._hunter_debug_continue_button: ft.Button | None = None
        self._hunter_chrome_cdp_pause_button: ft.Button | None = None
        self._hunter_chrome_normal_pause_button: ft.Button | None = None
        self._chrome_validate_pause_url: str | None = None
        self._hunter_service_ref: HunterService | None = None
        self._hunter_pause_strip: ft.Container | None = None
        self._hunter_pause_text: ft.Text | None = None
        self._onboarding_save_button: ft.Button | None = None
        self._onboarding_save_hint: ft.Text | None = None

    def log(self, message: str) -> None:
        self.log_console.value = (self.log_console.value + f"\n- {message}").strip()
        self.page.update()

    def _session_file_ready(self) -> bool:
        p = self.project_root / "session.json"
        return p.is_file() and p.stat().st_size > 16

    def _hunter_headless_step_debug_enabled(self) -> bool:
        return bool((self.config.get("hunter") or {}).get("headless_step_debug", False))

    def _hunter_pre_seat_validation_enabled(self) -> bool:
        return bool((self.config.get("hunter") or {}).get("pre_seat_visual_validation", False))

    def _hunter_needs_continue_button(self) -> bool:
        return self._hunter_headless_step_debug_enabled() or self._hunter_pre_seat_validation_enabled()

    def _refresh_chrome_validate_pause_buttons(self) -> None:
        if self._hunter_chrome_cdp_pause_button is None or self._hunter_chrome_normal_pause_button is None:
            return
        can_use = bool(
            self._hunter_running
            and self._chrome_validate_pause_url
            and self._hunter_needs_continue_button()
        )
        self._hunter_chrome_cdp_pause_button.disabled = not can_use
        self._hunter_chrome_normal_pause_button.disabled = not can_use
        tip = self._chrome_validate_pause_url or "Se habilita en cada pausa del hunter (misma URL que el headless)."
        self._hunter_chrome_cdp_pause_button.tooltip = tip
        self._hunter_chrome_normal_pause_button.tooltip = tip

    def _set_chrome_validate_pause_url(self, url: str | None) -> None:
        self._chrome_validate_pause_url = (url or "").strip() or None
        self._refresh_chrome_validate_pause_buttons()
        self.page.update()

    def _spawn_chrome_pause_subprocess(self, *, use_cdp_profile: bool) -> None:
        """Solo lanza Chrome (tras close_headless en _chrome_validation_pause_async)."""
        pause_url = self._chrome_validate_pause_url
        if not pause_url:
            self.log("Validacion pausa: aun no hay URL (esperando checkpoint).")
            return
        if not CHROME_PATH.exists():
            self.log("No se encontro Chrome en la ruta esperada.")
            return
        if use_cdp_profile:
            profile = _chrome_profile_dir_from_cfg(self.config)
            prof_path = Path(profile)
            if not prof_path.exists():
                self.log(f"Validacion pausa: no existe el perfil CDP {profile}")
                return
            subprocess.Popen(
                [str(CHROME_PATH), f"--user-data-dir={str(prof_path.resolve())}", "--new-window", pause_url]
            )
            self.log(
                "Dashboard: Chrome CDP abierto (Playwright ya cerrado). "
                "Importante: esta ventana usa cookies del PERFIL EN DISCO (carpeta CDP), no el archivo session.json. "
                "Playwright solo lee session.json; por eso puede pedir login de Google/FIFA aunque el hunter acabara de entrar. "
                "Para ver exactamente la misma sesion que el hunter sin otro Chrome: hunter.playwright_headless=false "
                "(ventana de Chromium del hunter)."
            )
        else:
            subprocess.Popen([str(CHROME_PATH), "--new-window", pause_url])
            self.log(
                "Dashboard: Chrome normal con URL de la pausa (Playwright ya se cerro). "
                "Perfil del sistema: no usa session.json; login y captchas son normales."
            )

    async def _chrome_validation_pause_async(self, use_cdp_profile: bool) -> None:
        """Cierra Chromium del hunter y abre Chrome visible (sin sesion paralela)."""
        h = self._hunter_service_ref
        if h is not None:
            await h.close_headless_for_external_chrome()
        self._spawn_chrome_pause_subprocess(use_cdp_profile=use_cdp_profile)

    def _schedule_chrome_validation_pause(self, use_cdp_profile: bool) -> None:
        """Flet run_task exige una funcion async, no un objeto coroutine ya creado."""

        async def _run() -> None:
            await self._chrome_validation_pause_async(use_cdp_profile)

        self.page.run_task(_run)

    def _request_chrome_pause_validation(self, *, use_cdp_profile: bool) -> None:
        self._schedule_chrome_validation_pause(use_cdp_profile)

    def _refresh_hunter_button_state(self) -> None:
        if self._hunter_button is not None:
            self._hunter_button.disabled = self._hunter_running
            self._hunter_button.text = "Caceria ejecutando..." if self._hunter_running else "Iniciar caceria (hunter)"
        if self._hunter_debug_continue_button is not None:
            needs = self._hunter_needs_continue_button()
            self._hunter_debug_continue_button.visible = True
            self._hunter_debug_continue_button.disabled = not self._hunter_running or not needs
        self._refresh_chrome_validate_pause_buttons()
        self.page.update()

    def _set_hunter_pause_strip(self, visible: bool, message: str = "") -> None:
        if self._hunter_pause_strip is None or self._hunter_pause_text is None:
            return
        self._hunter_pause_strip.visible = visible
        self._hunter_pause_text.value = message
        self.page.update()

    def _open_checkpoint_continue_dialog(
        self,
        *,
        step_id: str,
        pause_sec: Any,
        bot_wall: Any,
        datadome: Any,
        list_url: str,
    ) -> None:
        """Pausa con validacion visual: Chrome opcional, luego Continuar reanuda el headless."""
        if self._hunter_debug_continue is None:
            return

        self._set_chrome_validate_pause_url(list_url)

        profile = str(_chrome_profile_dir_from_cfg(self.config))

        def close_and_continue(_: ft.ControlEvent) -> None:
            if self._hunter_debug_continue is not None:
                self._hunter_debug_continue.set()
            dlg.open = False
            self._set_hunter_pause_strip(False, "")
            self._set_chrome_validate_pause_url(None)
            self.log("Continuar hunter: dialogo de checkpoint.")
            self.page.update()

        def copy_url(_: ft.ControlEvent) -> None:
            if list_url.strip():
                self.page.set_clipboard(list_url)
                self.log("Checkpoint: URL copiada al portapapeles.")

        def open_chrome_default(_: ft.ControlEvent) -> None:
            self._schedule_chrome_validation_pause(False)

        def open_chrome_profile(_: ft.ControlEvent) -> None:
            self._schedule_chrome_validation_pause(True)

        url_field = ft.TextField(
            label="URL actual del headless (validar aqui en Chrome)",
            value=list_url,
            read_only=True,
            multiline=True,
            min_lines=2,
            max_lines=4,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Hunter en pausa — validar en Chrome"),
            content=ft.Container(
                width=480,
                content=ft.Column(
                    [
                        ft.Text(f"Checkpoint: {step_id}", weight=ft.FontWeight.W_600, size=16),
                        ft.Text(f"bot_wall={bot_wall} | datadome_iframe={datadome}", size=12),
                        ft.Text(
                            "Los botones Chrome cierran primero Playwright y abren Chrome con esta URL "
                            "(evita sesion paralela). Luego pulse Continuar para reabrir Playwright.",
                            size=12,
                        ),
                        ft.Text(
                            f"Si no pulsas Continuar, el motor sigue solo tras ~{pause_sec}s.",
                            size=11,
                            color=ft.Colors.GREY_700,
                        ),
                        url_field,
                        ft.Row(
                            [
                                ft.TextButton("Copiar URL", on_click=copy_url),
                                ft.TextButton("Chrome perfil CDP", on_click=open_chrome_profile),
                                ft.TextButton("Chrome normal", on_click=open_chrome_default),
                            ],
                            tight=True,
                            wrap=True,
                        ),
                        ft.Text(
                            "Luego pulse «Continuar hunter» (aqui o el boton naranja del Dashboard).",
                            size=12,
                        ),
                    ],
                    tight=True,
                    spacing=8,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.Button(
                    "Continuar hunter (reanudar headless)",
                    on_click=close_and_continue,
                    bgcolor=ft.Colors.DEEP_ORANGE_700,
                    color=ft.Colors.WHITE,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.dialog = dlg
        dlg.open = True
        self._set_hunter_pause_strip(
            True,
            f"EN PAUSA: {step_id} — dialogo: abra Chrome para validar, luego Continuar.",
        )
        self.page.update()

    def _refresh_onboarding_save_state(self) -> None:
        if self._onboarding_save_button is None:
            return
        ready = self._session_file_ready()
        self._onboarding_save_button.disabled = not ready
        if self._onboarding_save_hint is not None:
            self._onboarding_save_hint.value = (
                "session.json detectado: ya puedes guardar y abrir Dashboard."
                if ready
                else "Primero captura session.json correctamente para habilitar el guardado."
            )
            self._onboarding_save_hint.color = ft.Colors.GREEN_400 if ready else ft.Colors.AMBER_300
        self.page.update()

    def run(self) -> None:
        allowed, country = check_geolocation_allowed()
        if not allowed:
            self.page.add(
                ft.Container(
                    padding=20,
                    content=ft.Text(
                        f"Acceso bloqueado por geolocalizacion. Pais detectado: {country}. Permitido: Mexico, United States o Canada."
                    ),
                )
            )
            self.page.update()
            return

        self.log(f"Geolocalizacion validada: {country}")
        self.show_onboarding()

    def _configure_window(self) -> None:
        self.page.window.width = 602
        self.page.window.height = 820
        self.page.window.min_width = 550
        self.page.window.min_height = 760
        self.page.window.max_width = 704
        self.page.window.max_height = 920
        self.page.window.maximizable = False

    def show_onboarding(self) -> None:
        self.page.title = "Biting Lobster - Onboarding"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.scroll = ft.ScrollMode.AUTO
        self._configure_window()
        self.config = self.config_repo.load()

        disclaimer = ft.Text(
            "Aviso de privacidad: esta app no solicita credenciales FIFA ni datos bancarios. "
            "Automatiza asistencia operativa y requiere interaccion manual para login/captcha.",
            size=12,
            color=ft.Colors.GREY_400,
        )

        chrome_profile_flag_text = ft.Text(size=12, color=ft.Colors.GREY_400)

        def refresh_chrome_profile_flag_label() -> None:
            flag = bool((self.config.get("app") or {}).get("requires_new_chrome_profile", False))
            fixed = _chrome_profile_dir_from_cfg(self.config)
            chrome_profile_flag_text.value = (
                "Perfil CDP: efimero (carpeta nueva en cada inicio) — app.requires_new_chrome_profile=true."
                if flag
                else f"Perfil CDP: fijo en {fixed} — app.requires_new_chrome_profile=false."
            )

        refresh_chrome_profile_flag_label()

        hunter_for_proxy = dict(self.config.get("hunter") or {})
        px0 = dict(hunter_for_proxy.get("playwright_proxy") or {})
        proxy_server_field = ft.TextField(
            label="Proxy servidor (host:puerto; vacío = sin proxy en Chrome CDP / hunter)",
            value=str(px0.get("server") or ""),
        )
        proxy_username_field = ft.TextField(
            label="Proxy usuario (pegar en el diálogo de Chrome si pide credenciales)",
            value=str(px0.get("username") or ""),
        )
        proxy_password_field = ft.TextField(
            label="Proxy contraseña",
            value=str(px0.get("password") or ""),
            password=True,
            can_reveal_password=True,
        )

        def persist_playwright_proxy_fields() -> None:
            cur = self.config_repo.load()
            hn = dict(cur.get("hunter") or {})
            prev = dict(hn.get("playwright_proxy") or {})
            hn["playwright_proxy"] = {
                **prev,
                "server": (proxy_server_field.value or "").strip(),
                "username": (proxy_username_field.value or "").strip(),
                "password": (proxy_password_field.value or "").strip(),
            }
            self.config = self.config_repo.update({"hunter": hn})

        def open_chrome_cdp(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome instalado en la ruta esperada.")
                return
            now = time.monotonic()
            if now - self._chrome_onboarding_last_launch < 2.5:
                self.log("Chrome ya se lanzo hace pocos segundos (doble clic o evento duplicado); ignorado.")
                return
            self._chrome_onboarding_last_launch = now
            persist_playwright_proxy_fields()
            self.log("Cerrando Chrome CDP previo para mantener integridad")
            try:
                _kill_windows_chrome_cdp_port_9222()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Aviso al cerrar Chrome CDP previo: {exc}")
            time.sleep(0.9)
            self.config = self.config_repo.load()
            cfg = self.config
            use_ephemeral = bool((cfg.get("app") or {}).get("requires_new_chrome_profile", False))
            if use_ephemeral:
                runs_root = _chrome_runs_dir_from_cfg(cfg)
                runs_root.mkdir(parents=True, exist_ok=True)
                profile_dir = runs_root / f"bl_{time.time_ns()}"
                profile_dir.mkdir(parents=False)
                user_data_arg = str(profile_dir)
            else:
                profile_fixed = _chrome_profile_dir_from_cfg(cfg)
                user_data_arg = str(profile_fixed)
            self.log(f"Iniciando Chrome CDP con perfil {user_data_arg}")
            hunter = dict(cfg.get("hunter") or {})
            attach_hunter = bool(hunter.get("attach_hunter_to_chrome_cdp", False))
            chrome_extra = chrome_extra_args_from_hunter_cfg(hunter)
            hint = chrome_cdp_manual_proxy_auth_hint(hunter)
            if hint:
                self.log(hint)
            start_url = TICKETS_HOME_URL if attach_hunter else "about:blank"
            subprocess.Popen(
                [
                    str(CHROME_PATH),
                    "--remote-debugging-port=9222",
                    f"--user-data-dir={user_data_arg}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--new-window",
                    *chrome_extra,
                    start_url,
                ]
            )
            if attach_hunter:
                self.log(
                    "Chrome CDP lanzado (modo attach hunter: URL inicial tienda FIFA; "
                    "sin higiene cookies/about:blank — no destruir la pestaña que usara el hunter)."
                )

                async def _cdp_wait_only() -> None:
                    ok = await asyncio.to_thread(wait_cdp_http_ready, CDP_ENDPOINT, 45.0)
                    if ok:
                        self.log("CDP 9222 listo (attach hunter: Chrome sigue en la tienda).")
                    else:
                        self.log("CDP 9222: sin respuesta HTTP (json/version); compruebe firewall o reintente.")

                self.page.run_task(_cdp_wait_only)
            else:
                self.log(
                    "Chrome CDP lanzado (URL inicial about:blank). En segundo plano: espera depurador 9222, "
                    "limpia cookies .fifa.com/.tickets.fifa.com del perfil y confirma about:blank. "
                    f"Luego abra la tienda manualmente o pegue: {TICKETS_HOME_URL}"
                )

                async def _cdp_post_launch_hygiene() -> None:
                    ok = await asyncio.to_thread(wait_cdp_http_ready, CDP_ENDPOINT, 45.0)
                    if not ok:
                        self.log(
                            "CDP 9222: sin respuesta HTTP (json/version) tras lanzar Chrome; "
                            "compruebe firewall o reintente."
                        )
                        return

                    def _hygiene() -> tuple[int, list[str]]:
                        return onboarding_cdp_startup_hygiene(CDP_ENDPOINT)

                    try:
                        removed, lines = await asyncio.to_thread(_hygiene)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"Onboarding CDP (higiene cookies/blank): {exc}")
                        return
                    self.log(
                        f"Onboarding CDP listo: cookies FIFA eliminadas del perfil ({removed} entradas); "
                        f"detalle: {', '.join(lines)}. Abra la tienda FIFA en esta ventana para iniciar sesion."
                    )

                self.page.run_task(_cdp_post_launch_hygiene)
            self.log(
                "Si ve «acceso restringido temporalmente», cierre Chrome y use «Limpiar y usar nuevo perfil» "
                "o espere antes de reintentar."
            )

        def open_chrome_normal(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome instalado en la ruta esperada.")
                return
            subprocess.Popen([str(CHROME_PATH), "--new-window", TICKETS_HOME_URL])
            self.log("Chrome normal: perfil por defecto del sistema (sin CDP).")

        def open_fifa_com_in_cdp_new_tab(_: ft.ControlEvent) -> None:
            """Nueva pestaña en el Chrome CDP del onboarding (9222), no otra instancia del sistema."""

            async def _open_async() -> None:
                def _run() -> None:
                    open_url_in_cdp_new_tab(CDP_ENDPOINT, FIFA_COM_TICKETS_PAGE)

                try:
                    await asyncio.to_thread(_run)
                except Exception as exc:  # noqa: BLE001
                    self.log(
                        f"No se pudo abrir fifa.com en Chrome CDP (¿«Iniciar Chrome (CDP 9222)» activo en {CDP_ENDPOINT})? "
                        f"{exc}"
                    )
                    return
                self.log(
                    f"Chrome CDP: fifa.com abierta en el perfil CDP (reutiliza about:blank si aplica). "
                    f"Inicio URL: {FIFA_COM_TICKETS_PAGE[:100]}..."
                )

            self.page.run_task(_open_async)

        def limpiar_y_nuevo_perfil_cdp(_: ft.ControlEvent) -> None:
            self.config = self.config_repo.load()
            profile = _chrome_profile_dir_from_cfg(self.config)
            self.log("Cerrando Chrome CDP (remote-debugging-port=9222) si estaba abierto...")
            try:
                _kill_windows_chrome_cdp_port_9222()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Aviso al cerrar Chrome CDP: {exc}")
            time.sleep(1.5)
            try:
                if profile.exists():
                    shutil.rmtree(profile, ignore_errors=True)
                profile.mkdir(parents=True, exist_ok=True)
                self.log(
                    f"Listo: carpeta de perfil CDP recreada vacia (Chrome no se inicio): {profile}. "
                    "app.requires_new_chrome_profile=false en config.yaml."
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"ERROR recreando perfil CDP: {exc}")
                self.page.update()
                return
            self.config = self.config_repo.update({"app": {"requires_new_chrome_profile": False}})
            refresh_chrome_profile_flag_label()
            self.page.update()

        async def onboarding_cdp_poll_loop() -> None:
            cancel = self._onboarding_poll_cancel
            if cancel is None:
                return
            while True:
                for _ in range(25):
                    if cancel.is_set():
                        return
                    await asyncio.sleep(1)
                if cancel.is_set():
                    return
                if (self.config.get("app") or {}).get("requires_new_chrome_profile"):
                    continue
                try:

                    def run_probe() -> bool:
                        return detect_queue_restriction_via_cdp()

                    hit = await asyncio.to_thread(run_probe)
                except Exception:
                    continue
                if hit:
                    self.config = self.config_repo.update({"app": {"requires_new_chrome_profile": True}})
                    refresh_chrome_profile_flag_label()
                    self.log(
                        "Detectado selectqueue + acceso restringido; app.requires_new_chrome_profile=true "
                        "(guardado en config.yaml)."
                    )
                    self.page.update()

        step1 = ft.Container(
            padding=10,
            content=ft.Column(
                [
                    ft.Text("Paso 1 - Descarga e instala Google Chrome"),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Descargar Chrome",
                                on_click=lambda e: webbrowser.open("https://www.google.com/chrome/"),
                            ),
                            ft.OutlinedButton("Iniciar Chrome (CDP 9222)", on_click=open_chrome_cdp),
                            ft.Button("Iniciar Chrome Normal", on_click=open_chrome_normal),
                            ft.Button(
                                "Abrir en nueva pestaña fifa.com",
                                on_click=open_fifa_com_in_cdp_new_tab,
                                tooltip="Abre fifa.com en Chrome CDP: reutiliza la pestaña si está en about:blank; si no, abre una pestaña nueva.",
                            ),
                            ft.Button(
                                "Limpiar y usar nuevo perfil en Chrome",
                                on_click=limpiar_y_nuevo_perfil_cdp,
                            ),
                        ],
                        wrap=True,
                    ),
                    chrome_profile_flag_text,
                    ft.Text(
                        "Proxy (hunter.playwright_proxy): si falta servidor, usuario o contraseña, "
                        "no se usa --proxy-server en Chrome CDP ni proxy en el hunter.",
                        size=11,
                        color=ft.Colors.GREY_500,
                    ),
                    proxy_server_field,
                    proxy_username_field,
                    proxy_password_field,
                    ft.Text(
                        "Deteccion automatica cada ~25 s (Chrome CDP en 9222 abierto): si alguna pestaña FIFA "
                        "(tienda *.tickets.fifa.com o cola access.tickets.fifa.com) muestra en el cuerpo "
                        "«acceso restringido temporalmente», se escribe requires_new_chrome_profile=true en config.yaml.",
                        size=11,
                        color=ft.Colors.GREY_500,
                    ),
                    ft.Text(
                        "Iniciar Chrome (CDP) usa la ruta app.biting_lobster_chrome_profile en config.yaml "
                        "(carpeta aparte del Chrome normal). "
                        "No es el mismo perfil que el icono habitual de Chrome: no hereda tu inicio de sesion "
                        "ni el paso por cola que ya hiciste en el otro navegador. Hasta entrar aqui, "
                        "FIFA puede redirigir a access.tickets.fifa.com (cola PKP); es el flujo normal para "
                        "ese perfil, no un rastro corrupto en disco.",
                        size=12,
                        color=ft.Colors.GREY_400,
                    ),
                    ft.Text(
                        "Si en esa cola ves «El acceso está restringido temporalmente», suele ser limitacion "
                        "del lado FIFA o dos ventanas con la tienda a la vez (esta y Chrome normal). "
                        "Prueba cerrar la pestaña de la tienda en Chrome del sistema, esperar unos minutos "
                        "y repetir solo en esta ventana CDP. Chrome con CDP puede además mostrar aviso de "
                        "depuración remota (mensaje del navegador).",
                        size=12,
                        color=ft.Colors.GREY_400,
                    ),
                ]
            ),
        )

        session_path = self.project_root / "session.json"

        async def capture_session_click() -> None:
            self.config = self.config_repo.load()
            hunter = dict(self.config.get("hunter") or {})
            try:
                progress_lines: list[str] = []

                def progress_log(msg: str) -> None:
                    progress_lines.append(msg)

                def run_capture() -> dict[str, Any]:
                    mgr = SessionManager(session_file=str(session_path))
                    return mgr.capture_session(
                        hunter_cfg=hunter,
                        capture_via_ui=True,
                        progress_log=progress_log,
                    )

                result = await asyncio.to_thread(run_capture)
                for line in progress_lines:
                    self.log(line)
            except Exception as exc:  # noqa: BLE001
                self.log(f"ERROR: no se pudo guardar session.json - {exc}")
                self.page.update()
                return

            saved = Path(str(result.get("session_file", session_path)))
            self.log(f"OK: session.json guardado en {saved.resolve()}")
            val = result.get("validation") or {}
            self.log(
                "Validacion perfil FIFA: "
                f"hogar_limite_4={val.get('household_limit_detected')}, "
                f"restriccion_diaria={val.get('daily_restriction_detected')}"
            )
            if saved.is_file():
                self.log(f"Verificado en disco: {saved.name} ({saved.stat().st_size} bytes). Listo para hunter_smoke.py.")
            else:
                self.log(f"AVISO: no se encontro {saved.name} en disco tras la captura.")
            self._refresh_onboarding_save_state()

        async def capture_session_firefox_click() -> None:
            self.config = self.config_repo.load()
            hunter = dict(self.config.get("hunter") or {})
            app_cfg = dict(self.config.get("app") or {})
            self.log("Captura Firefox/Camoufox: abriendo ventana (Playwright); complete login en esa ventana...")
            try:
                progress_lines_ff: list[str] = []

                def progress_log_ff(msg: str) -> None:
                    progress_lines_ff.append(msg)

                def run_capture() -> dict[str, Any]:
                    mgr = SessionManager(session_file=str(session_path))
                    return mgr.capture_session_firefox(
                        hunter_cfg=hunter,
                        app_cfg=app_cfg,
                        capture_via_ui=True,
                        progress_log=progress_log_ff,
                    )

                result = await asyncio.to_thread(run_capture)
                for line in progress_lines_ff:
                    self.log(line)
            except Exception as exc:  # noqa: BLE001
                self.log(f"ERROR: captura Firefox - {exc}")
                self.page.update()
                return

            saved = Path(str(result.get("session_file", session_path)))
            self.log(f"OK: session.json guardado en {saved.resolve()} (modo={result.get('mode')}, exe={result.get('executable')})")
            val = result.get("validation") or {}
            self.log(
                "Validacion perfil FIFA: "
                f"hogar_limite_4={val.get('household_limit_detected')}, "
                f"restriccion_diaria={val.get('daily_restriction_detected')}"
            )
            if saved.is_file():
                self.log(
                    f"Verificado en disco: {saved.name} ({saved.stat().st_size} bytes). "
                    "Cacería: Chromium+stealth si hunter.use_camoufox: false (recomendado); true solo si usa Camoufox."
                )
            self._refresh_onboarding_save_state()

        step2 = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Paso 2 - Inicie sesion en la tienda FIFA (Chrome CDP ya abierto, o ventana Firefox al capturar)."),
                    ft.Text(
                        "No olvides validar Captcha y cualquier codigo enviado a tu correo electronico.",
                        size=13,
                        color=ft.Colors.GREY_300,
                    ),
                    ft.Text(
                        "Opcion A — Chrome: pulse «Iniciar Chrome (CDP 9222)» en el paso 1, espere el log «Onboarding CDP listo» "
                        "(limpia cookies FIFA del perfil y deja about:blank), abra la tienda en esa ventana, luego "
                        "«Capturar (CDP)» (Chrome debe seguir abierto). "
                        "Opcion B — Firefox/Camoufox: pulse «Capturar (Firefox)» y haga login en la ventana que abre Playwright "
                        "(no usa puerto 9222; adecuado si hunter.use_camoufox: true). Usa hunter.camoufox_executable en config.",
                        size=12,
                        color=ft.Colors.GREY_400,
                    ),
                    ft.Row(
                        [
                            ft.Button(
                                "Capturar y guardar session.json (CDP)",
                                on_click=lambda e: self.page.run_task(capture_session_click),
                            ),
                            ft.Button(
                                "Capturar y guardar session.json (Firefox / Camoufox)",
                                on_click=lambda e: self.page.run_task(capture_session_firefox_click),
                            ),
                        ],
                        wrap=True,
                    ),
                ]
            ),
            padding=10,
        )

        selected_teams = self.config.get("search_criteria", {}).get("target_teams", [])
        team_dropdown = ft.Dropdown(
            label="Equipos objetivo (ID FIFA)",
            options=[ft.dropdown.Option(key=team_id, text=f"{label} ({team_id})") for team_id, label in TEAM_OPTIONS],
            value=selected_teams[0] if selected_teams else TEAM_OPTIONS[0][0],
            menu_height=360,
        )

        max_price_cents_cfg = int(self.config.get("search_criteria", {}).get("max_price_cents", 25000))
        max_price_usd_default = f"{(max_price_cents_cfg / 100):.2f}"
        max_price_field = ft.TextField(label="Limite de precio (USD)", value=max_price_usd_default)
        quantity_field = ft.TextField(
            label="Cantidad de boletos deseada",
            value=str(self.config.get("search_criteria", {}).get("quantity", 1)),
        )
        quantity_warning = ft.Text("", size=12, color=ft.Colors.AMBER_300)

        preferred_categories_raw = self.config.get("search_criteria", {}).get("preferred_categories", [2, 3, 4, 1])
        preferred_categories: list[int] = []
        if isinstance(preferred_categories_raw, list):
            for item in preferred_categories_raw:
                try:
                    preferred_categories.append(int(item))
                except (TypeError, ValueError):
                    continue
        if not preferred_categories:
            preferred_categories = [2, 3, 4, 1]
        category_controls = [
            ft.Dropdown(
                label=f"Prioridad categoria #{idx + 1}",
                options=[ft.dropdown.Option(str(c), f"Categoria {c}") for c in CATEGORY_OPTIONS],
                value=str(preferred_categories[idx]) if idx < len(preferred_categories) else str(CATEGORY_OPTIONS[idx]),
            )
            for idx in range(4)
        ]

        converter = CurrencyConverter(self.config)
        mxn_rate = converter.rates.get("MXN", 0)
        cad_rate = converter.rates.get("CAD", 0)
        preview_text = ft.Text(
            f"Referencia de tipo de cambio: 1 USD = {mxn_rate} MXN | 1 USD = {cad_rate} CAD",
            size=12,
            color=ft.Colors.GREY_400,
        )

        def update_conversion_preview(_: ft.ControlEvent) -> None:
            try:
                amount = float(max_price_field.value or "0")
                cents = converter.to_usd_cents(amount, "USD")
                preview_text.value = (
                    f"Referencia de tipo de cambio: 1 USD = {mxn_rate} MXN | 1 USD = {cad_rate} CAD "
                    f"(equivale a {cents} cents USD)"
                )
            except ValueError:
                preview_text.value = "Valor de limite invalido."
            self.page.update()

        max_price_field.on_change = update_conversion_preview

        def update_quantity_warning(_: ft.ControlEvent) -> None:
            try:
                quantity = int(quantity_field.value or "1")
                if quantity > 4:
                    quantity_warning.value = "Cantidades por arriba de 4 pueden causar el bloqueo de la aplicacion."
                else:
                    quantity_warning.value = ""
            except ValueError:
                quantity_warning.value = "Cantidad invalida."
            self.page.update()

        quantity_field.on_change = update_quantity_warning

        keep_chrome_on_dashboard_switch = ft.Switch(
            label="Diagnostico: no cerrar Chrome CDP al abrir Dashboard (deja la ventana abierta para observar)",
            value=bool((self.config.get("app") or {}).get("keep_chrome_cdp_open_on_save_to_dashboard", False)),
        )
        keep_chrome_diag_hint = ft.Text(
            "Si esta activo, al pulsar «Guardar y abrir Dashboard» no se mata el proceso en puerto 9222: "
            "puede ver la pestaña FIFA hasta donde llegue. Cierre Chrome usted antes del hunter si hace falta liberar 9222. "
            "Queda guardado en app.keep_chrome_cdp_open_on_save_to_dashboard en config.yaml.",
            size=10,
            color=ft.Colors.GREY_500,
        )

        def save_onboarding(_: ft.ControlEvent) -> None:
            if not self._session_file_ready():
                self.log("ERROR: captura session.json antes de guardar y abrir Dashboard.")
                self._refresh_onboarding_save_state()
                return
            team_val = team_dropdown.value
            if team_val is None or not str(team_val).strip():
                self.log("ERROR: selecciona al menos un equipo objetivo antes de guardar.")
                self.page.update()
                return

            try:
                limit_usd = float(max_price_field.value or "0")
            except ValueError:
                self.log("ERROR: limite de precio (USD) no es un numero valido.")
                self.page.update()
                return
            if limit_usd <= 0:
                self.log("ERROR: indica un limite de precio en USD mayor que 0.")
                self.page.update()
                return

            categories = [int(control.value) for control in category_controls]
            seen = set()
            ordered = []
            for c in categories:
                if c not in seen:
                    ordered.append(c)
                    seen.add(c)
            for fallback in CATEGORY_OPTIONS:
                if fallback not in seen:
                    ordered.append(fallback)

            max_price_cents = converter.to_usd_cents(limit_usd, "USD")
            quantity = max(1, int(quantity_field.value or "1"))

            start_on_boot = bool(self.config.get("app", {}).get("start_on_boot", False))
            keep_chrome_open = bool(keep_chrome_on_dashboard_switch.value)
            hunter_cfg = dict(self.config.get("hunter") or {})
            prev_px = dict(hunter_cfg.get("playwright_proxy") or {})
            hunter_cfg["playwright_proxy"] = {
                **prev_px,
                "server": (proxy_server_field.value or "").strip(),
                "username": (proxy_username_field.value or "").strip(),
                "password": (proxy_password_field.value or "").strip(),
            }
            hunter_cfg.setdefault("speed", "baja")
            hunter_cfg.setdefault("product_id", DEFAULT_PRODUCT_ID)
            hunter_cfg.setdefault("lang", "es")
            hunter_cfg.setdefault("seat_table_index", 1)
            hunter_cfg.setdefault("initial_delay_sec", 3.5)
            draft_search = {
                "target_teams": [str(team_val).strip()],
                "max_price_cents": max_price_cents,
                "quantity": quantity,
                "preferred_categories": ordered,
            }
            draft_cfg = {**self.config, "search_criteria": {**(self.config.get("search_criteria") or {}), **draft_search}}
            ok_obj, obj_msg = validate_hunter_search_objective(draft_cfg)
            if not ok_obj:
                self.log(f"ERROR: {obj_msg}")
                self.page.update()
                return

            updated = self.config_repo.update(
                {
                    "search_criteria": draft_search,
                    "app": {
                        "start_on_boot": start_on_boot,
                        "keep_chrome_cdp_open_on_save_to_dashboard": keep_chrome_open,
                    },
                    "hunter": hunter_cfg,
                }
            )
            self.config = updated
            self.log("Onboarding guardado en config.yaml.")
            attach_hunter = bool(hunter_cfg.get("attach_hunter_to_chrome_cdp", False))
            if attach_hunter:
                self.log(
                    "hunter.attach_hunter_to_chrome_cdp: no se cierra Chrome CDP al ir al Dashboard "
                    "(el hunter se conectara a esta instancia)."
                )
            elif keep_chrome_open:
                self.log(
                    "Diagnostico: Chrome CDP no se cerro — puede seguir viendo la pestaña. "
                    "Cierre Chrome manualmente antes de iniciar el hunter si Playwright necesita el puerto 9222 libre."
                )
            else:
                self.log("Cerrando Chrome CDP (puerto 9222) si estaba abierto...")
                try:
                    _kill_windows_chrome_cdp_port_9222()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Aviso al cerrar Chrome CDP: {exc}")
                time.sleep(0.8)
            self.show_dashboard()

        save_hint = ft.Text(
            "Primero captura session.json en el Paso 2.",
            size=12,
            color=ft.Colors.AMBER_300,
        )
        save_button = ft.Button("Guardar y abrir Dashboard", on_click=save_onboarding)

        step3 = ft.Container(
            padding=10,
            content=ft.Column(
                [
                    ft.Text("Paso 3 - Seleccion de criterios"),
                    team_dropdown,
                    ft.Text("Categorias con prioridad (1..4)"),
                    *category_controls,
                    max_price_field,
                    preview_text,
                    quantity_field,
                    quantity_warning,
                    save_hint,
                    keep_chrome_on_dashboard_switch,
                    keep_chrome_diag_hint,
                    save_button,
                ]
            ),
        )

        # Keep refs for UX state updates.
        self._onboarding_save_hint = save_hint
        self._onboarding_save_button = save_button

        self.page.controls.clear()
        self.page.add(
            ft.Container(
                width=563,
                padding=10,
                content=ft.Column(
                    [
                        step1,
                        step2,
                        step3,
                        ft.Divider(),
                        ft.Text("LogConsole", size=12, weight=ft.FontWeight.W_500),
                        ft.Container(height=240, content=self.log_console),
                        ft.Divider(),
                        disclaimer,
                    ],
                    tight=False,
                ),
            )
        )
        self.page.update()
        self._refresh_onboarding_save_state()
        self._onboarding_poll_cancel = asyncio.Event()
        self.page.run_task(onboarding_cdp_poll_loop)

    def show_dashboard(self) -> None:
        if self._onboarding_poll_cancel is not None:
            self._onboarding_poll_cancel.set()
            self._onboarding_poll_cancel = None
        self.config = self.config_repo.load()
        self.page.title = "Biting Lobster - Dashboard"
        self._configure_window()
        start_boot_switch = ft.Switch(
            label="Iniciar con sistema (Windows)",
            value=bool(self.config.get("app", {}).get("start_on_boot", False)),
        )
        license_text = ft.Text("Licencia: sincronizando...")

        def on_start_boot_change(_: ft.ControlEvent) -> None:
            enabled = bool(start_boot_switch.value)
            set_start_on_boot(enabled, self.project_root)
            self.config = self.config_repo.update({"app": {"start_on_boot": enabled}})
            self.log(f"Inicio con sistema (Windows) => {enabled}")

        start_boot_switch.on_change = on_start_boot_change

        hardware_row = ft.Row(
            [
                ft.Text(f"Hardware ID: {self.hardware_id}", selectable=True),
                ft.TextButton("Copiar", on_click=lambda e: self.page.set_clipboard(self.hardware_id)),
            ]
        )

        async def poll_licenses() -> None:
            self.polling_active = True
            license_data = self.license_repo.upsert_license(self.hardware_id)
            now = time.strftime("%H:%M:%S")
            is_fallback = license_data.get("source") == "local_fallback"
            previous_access = str(license_data.get("access_granted", "LIMITED")).upper()
            previous_connected = not is_fallback
            self._set_supabase_status(
                "Supabase: fallback local (sin conexion o credenciales invalidas)"
                if is_fallback
                else "Supabase: conectado"
            )
            self._set_last_sync(now)
            self.log(f"Licencia inicial: {license_data.get('access_granted', 'LIMITED')}")
            while self.polling_active:
                data = self.license_repo.get_license(self.hardware_id) or license_data
                is_connected = data is not None and data.get("source") != "local_fallback"
                access = str(data.get("access_granted", "LIMITED")).upper()
                max_tickets = 40 if access == "FULL" else 1
                now = time.strftime("%H:%M:%S")
                self._set_dashboard_status(
                    "Supabase: conectado"
                    if is_connected
                    else "Supabase: fallback local (sin conexion o credenciales invalidas)",
                    f"Licencia: {access} | max_tickets_secured={max_tickets}",
                    license_text,
                    now,
                )

                if is_connected != previous_connected:
                    self.log(
                        "Supabase conectado."
                        if is_connected
                        else "Supabase en fallback local (sin conexion/credenciales)."
                    )
                    previous_connected = is_connected

                if access != previous_access:
                    self.log(f"Licencia actualizada: {previous_access} -> {access}")
                    previous_access = access
                await asyncio.sleep(30)

        hunter_hint = ft.Text(
            "Caceria: Playwright headless + session.json. Si aparece DataDome, se abrira un dialogo con la URL "
            "y pasos para resolver en Chrome y volver a capturar session.json.",
            size=11,
            color=ft.Colors.GREY_400,
        )
        hunter_continue_hint = ft.Text(
            "Pausa: «Chrome CDP» usa el perfil en disco (puede pedir login aunque Playwright ya entrara: session.json no se "
            "inyecta a esa carpeta). «Chrome normal» es otro perfil. Para ver la MISMA sesion que el hunter: "
            "hunter.playwright_headless=false (ventana Playwright). Los botones Chrome cierran Playwright antes; "
            "luego «>>> Continuar hunter <<<» reabre con session.json. «Iniciar caceria» se deshabilita durante la corrida.",
            size=10,
            color=ft.Colors.GREY_500,
        )
        hunter_button = ft.Button(
            "Iniciar caceria (hunter)",
            on_click=lambda _e: self.page.run_task(self._run_hunter_async),
        )
        self._hunter_button = hunter_button

        def on_hunter_debug_continue(_: ft.ControlEvent) -> None:
            if self._hunter_debug_continue is not None:
                self._hunter_debug_continue.set()
            self._set_hunter_pause_strip(False, "")
            self._set_chrome_validate_pause_url(None)
            self.log("Continuar hunter: reanudando (checkpoint o validacion listado).")

        self._hunter_debug_continue_button = ft.Button(
            ">>> Continuar hunter <<<",
            on_click=on_hunter_debug_continue,
            visible=True,
            disabled=True,
            style=ft.ButtonStyle(
                bgcolor={
                    ft.ControlState.DEFAULT: ft.Colors.DEEP_ORANGE_700,
                    ft.ControlState.DISABLED: ft.Colors.GREY_600,
                },
                color={
                    ft.ControlState.DEFAULT: ft.Colors.WHITE,
                    ft.ControlState.DISABLED: ft.Colors.WHITE,
                },
            ),
        )

        self._hunter_chrome_cdp_pause_button = ft.Button(
            "Chrome CDP — validar pausa",
            on_click=lambda _: self._request_chrome_pause_validation(use_cdp_profile=True),
            disabled=True,
            tooltip="Cierra Playwright y abre Chrome con perfil CDP y la URL de la pausa.",
        )
        self._hunter_chrome_normal_pause_button = ft.Button(
            "Chrome normal — validar pausa",
            on_click=lambda _: self._request_chrome_pause_validation(use_cdp_profile=False),
            disabled=True,
            tooltip="Cierra Playwright y abre Chrome del sistema (sin session.json).",
        )

        hunter_actions = ft.Row(
            [
                hunter_button,
                self._hunter_debug_continue_button,
                self._hunter_chrome_cdp_pause_button,
                self._hunter_chrome_normal_pause_button,
            ],
            alignment=ft.MainAxisAlignment.START,
            spacing=10,
            wrap=True,
        )

        self._hunter_pause_text = ft.Text(
            "",
            color=ft.Colors.WHITE,
            weight=ft.FontWeight.W_700,
            size=13,
        )
        self._hunter_pause_strip = ft.Container(
            visible=False,
            padding=10,
            bgcolor=ft.Colors.RED_900,
            content=self._hunter_pause_text,
        )

        self.page.controls.clear()
        self.page.add(
            ft.Container(
                width=563,
                padding=10,
                content=ft.Column(
                    [
                        self.status_text,
                        self.supabase_status_text,
                        self.last_sync_text,
                        hardware_row,
                        start_boot_switch,
                        license_text,
                        ft.Divider(height=12),
                        hunter_hint,
                        hunter_continue_hint,
                        hunter_actions,
                        self._hunter_pause_strip,
                        ft.Divider(height=12),
                        self.log_console,
                    ],
                    tight=True,
                    spacing=4,
                ),
            )
        )
        self.status_text.value = "Estado: Dashboard listo"
        self.page.update()
        self._refresh_hunter_button_state()
        self.page.run_task(poll_licenses)

    def _set_supabase_status(self, text: str) -> None:
        self.supabase_status_text.value = text
        self.page.update()

    def _set_last_sync(self, hhmmss: str) -> None:
        self.last_sync_text.value = f"Ultima sincronizacion: {hhmmss}"
        self.page.update()

    def _set_dashboard_status(
        self,
        supabase_text: str,
        license_text_value: str,
        license_label: ft.Text,
        hhmmss: str,
    ) -> None:
        self.supabase_status_text.value = supabase_text
        self.last_sync_text.value = f"Ultima sincronizacion: {hhmmss}"
        license_label.value = license_text_value
        # Force a visible repaint marker even on virtualized desktops.
        self.status_text.value = f"Estado: Dashboard listo ({hhmmss})"
        self.page.update()

    async def _on_hunter_service_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "captcha_handoff_required":
            await self._show_captcha_handoff_dialog(payload)
            return
        if event_type == "pre_seat_browser_validation":
            await self._show_pre_seat_validation_dialog(payload)
            return
        if event_type == "hunter_checkpoint":
            step = payload.get("step_id", "?")
            wall = payload.get("bot_wall")
            dd = payload.get("datadome_iframe_visible")
            snap = payload.get("session_snapshot") or "—"
            pause = payload.get("pause_sec")
            wait_btn = payload.get("wait_for_ui_continue")
            url = str(payload.get("url") or "")
            note = str(payload.get("note") or "")
            self.log(
                f"CHECKPOINT [{step}] bot_wall={wall} datadome_iframe={dd} "
                f"pausa_s={pause} espera_boton={wait_btn}\n"
                f"  {note}\n"
                f"  url={url[:200]}{'...' if len(url) > 200 else ''}\n"
                f"  snapshot={snap}"
            )
            if wait_btn and self._hunter_debug_continue is not None:
                self._open_checkpoint_continue_dialog(
                    step_id=str(step),
                    pause_sec=pause,
                    bot_wall=wall,
                    datadome=dd,
                    list_url=url,
                )
            return
        if event_type == "log":
            self.log(str(payload.get("message", "")))
            return
        if event_type == "error":
            self.log(f"ERROR: {payload.get('message', payload)}")
            return
        if event_type == "auth_required":
            self.log(f"Auth requerida (vuelva a capturar sesion): {payload.get('url', '')}")
            return
        if event_type == "ticket_secured":
            self.log(f"Boleto asegurado (revisar carrito): {payload.get('message', '')} | {payload}")
            return
        self.log(f"[{event_type}] {payload}")

    async def _show_captcha_handoff_dialog(self, payload: dict[str, Any]) -> None:
        url = str(payload.get("handoff_url") or "")
        step = str(payload.get("step") or "?")
        perf = payload.get("performance_id")
        teams = payload.get("target_teams") or []
        instr = str(payload.get("instructions_es") or "")

        url_field = ft.TextField(
            label="URL para abrir en Chrome (misma sesion que session.json)",
            value=url,
            read_only=True,
            multiline=True,
            min_lines=2,
            max_lines=5,
        )

        def close_dialog(_: ft.ControlEvent) -> None:
            dlg.open = False
            self.page.update()

        def copy_url(_: ft.ControlEvent) -> None:
            self.page.set_clipboard(url)
            self.log("Handoff captcha: URL copiada al portapapeles.")

        def open_chrome(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome en la ruta esperada.")
                return
            if not url.strip():
                self.log("Handoff: URL vacia.")
                return
            subprocess.Popen([str(CHROME_PATH), "--new-window", url])
            self.log("Handoff captcha: nueva ventana Chrome con la URL.")

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Handoff: Captcha / DataDome"),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    [
                        ft.Text(
                            "El hunter en headless no puede resolver esta pantalla. "
                            "Complete el paso en Chrome y vuelva a capturar session.json.",
                            size=13,
                        ),
                        ft.Text(f"Paso detectado: {step}", weight=ft.FontWeight.W_600),
                        ft.Text(f"Performance ID: {perf or '—'}"),
                        ft.Text(f"Equipos objetivo: {teams}", size=12, color=ft.Colors.GREY_400),
                        ft.Text(instr, selectable=True, size=13),
                        url_field,
                        ft.Row(
                            [
                                ft.TextButton("Copiar URL", on_click=copy_url),
                                ft.TextButton("Abrir en Chrome", on_click=open_chrome),
                            ],
                            tight=True,
                        ),
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[ft.TextButton("Entendido", on_click=close_dialog)],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    async def _show_pre_seat_validation_dialog(self, payload: dict[str, Any]) -> None:
        url = str(payload.get("list_url") or "")
        self._set_chrome_validate_pause_url(url)
        perf = payload.get("performance_id")
        timeout_sec = payload.get("timeout_sec")
        wall = payload.get("bot_wall_headless")
        dd = payload.get("datadome_iframe_headless")
        instr = str(payload.get("instructions_es") or "")

        url_field = ft.TextField(
            label="URL del listado (misma vista que el headless)",
            value=url,
            read_only=True,
            multiline=True,
            min_lines=2,
            max_lines=4,
        )

        def close_dialog_only(_: ft.ControlEvent) -> None:
            dlg.open = False
            self._set_hunter_pause_strip(False, "")
            self.page.update()

        def continue_after_pre_seat(_: ft.ControlEvent) -> None:
            if self._hunter_debug_continue is not None:
                self._hunter_debug_continue.set()
            dlg.open = False
            self._set_hunter_pause_strip(False, "")
            self._set_chrome_validate_pause_url(None)
            self.log("Continuar hunter: validacion pre-asientos (dialogo).")
            self.page.update()

        def copy_url(_: ft.ControlEvent) -> None:
            self.page.set_clipboard(url)
            self.log("Validacion listado: URL copiada al portapapeles.")

        def open_chrome_default(_: ft.ControlEvent) -> None:
            self._schedule_chrome_validation_pause(False)

        def open_chrome_profile(_: ft.ControlEvent) -> None:
            self._schedule_chrome_validation_pause(True)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Validar listado antes de asientos"),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    [
                        ft.Text(
                            "Playwright esta en pausa. Los botones Chrome cierran Playwright antes de abrir la ventana; "
                            "luego pulse «Continuar hunter» para seguir.",
                            size=13,
                        ),
                        ft.Text(
                            f"Playwright antes de pausa (referencia): bot_wall={wall} | datadome_iframe={dd} | tope_s={timeout_sec}",
                            size=12,
                            color=ft.Colors.GREY_400,
                        ),
                        ft.Text(f"Performance ID (siguiente paso): {perf or '—'}", size=12),
                        ft.Text(instr, selectable=True, size=12),
                        url_field,
                        ft.Row(
                            [
                                ft.TextButton("Copiar URL", on_click=copy_url),
                                ft.TextButton("Chrome perfil CDP", on_click=open_chrome_profile),
                                ft.TextButton("Chrome normal", on_click=open_chrome_default),
                            ],
                            tight=True,
                            wrap=True,
                        ),
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.Button(
                    "Continuar hunter (seguir a asientos)",
                    on_click=continue_after_pre_seat,
                    bgcolor=ft.Colors.DEEP_ORANGE_700,
                    color=ft.Colors.WHITE,
                ),
                ft.TextButton("Solo cerrar dialogo", on_click=close_dialog_only),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.dialog = dlg
        dlg.open = True
        self._set_hunter_pause_strip(
            True,
            "VALIDACION PRE-ASIENTOS: dialogo abierto — pulse «Continuar hunter» en el dialogo o el boton naranja.",
        )
        self.page.update()

    async def _run_hunter_async(self, _: ft.ControlEvent | None = None) -> None:
        if self._hunter_running:
            self.log("La caceria ya esta en ejecucion.")
            return
        session_path = self.project_root / "session.json"
        if not session_path.is_file():
            self.log(f"Falta {session_path.name}. Capture la sesion desde el onboarding.")
            return
        self.config = self.config_repo.load()
        ok, msg = validate_hunter_search_objective(self.config)
        if not ok:
            self.log(f"No se puede iniciar el hunter: {msg}")
            return

        hunter_cfg = dict(self.config.get("hunter") or {})
        if hunter_cfg.get("attach_hunter_to_chrome_cdp"):
            self.log(
                "Iniciando hunter (attach_hunter_to_chrome_cdp): deje Chrome CDP (9222) abierto con la tienda FIFA; "
                "Playwright se conectara a la pestaña existente (no cierre Chrome ni use Chromium paralelo)."
            )
        else:
            self.log(
                "Iniciando hunter: cierre Chrome CDP si estaba abierto; "
                "no use la misma sesion en dos navegadores a la vez."
            )
        step_debug = self._hunter_headless_step_debug_enabled()
        pre_seat = self._hunter_pre_seat_validation_enabled()
        self._hunter_debug_continue = asyncio.Event() if (step_debug or pre_seat) else None
        self._hunter_running = True
        self._refresh_hunter_button_state()
        if step_debug or pre_seat:
            self.log(
                "Hunter UI: use el boton «Continuar hunter» (debajo de «Iniciar caceria»). "
                "Si no lo ve, desplacese hacia abajo en el panel del Dashboard."
            )

        def dispatch_hunter_event(et: str, pl: dict[str, Any]) -> None:
            """HunterService corre en el mismo loop; los dialogos Flet deben ir con page.run_task."""

            async def handle() -> None:
                await self._on_hunter_service_event(et, pl)

            self.page.run_task(handle)

        try:
            hunter = HunterService(
                self.project_root,
                self.config,
                on_event=dispatch_hunter_event,
                debug_continue_event=self._hunter_debug_continue,
            )
            self._hunter_service_ref = hunter
            await hunter.run_loop()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Hunter: excepcion no controlada — {exc}")
        finally:
            self._hunter_service_ref = None
            self._hunter_running = False
            self._hunter_debug_continue = None
            self._set_chrome_validate_pause_url(None)
            self.log("Hunter: ejecucion finalizada.")
            self._refresh_hunter_button_state()


def main(page: ft.Page) -> None:
    app = DashboardApp(page)
    app.run()


if __name__ == "__main__":
    ft.run(main)
