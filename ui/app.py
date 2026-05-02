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
from core.HunterService import DEFAULT_PRODUCT_ID
from core.geo import check_geolocation_allowed
from core.hardware import get_hardware_id
from core.startup_windows import set_start_on_boot
from data.ConfigRepository import ConfigRepository
from data.LicenseRepository import LicenseRepository
from data.SessionManager import SessionManager, TICKETS_HOME_URL
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
# Pagina informativa en fifa.com; CDP abre TICKETS_HOME_URL (tienda).
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
            min_lines=12,
            max_lines=20,
            read_only=True,
            value="",
            expand=True,
        )
        self.status_text = ft.Text("Estado: inicializando...")
        self.supabase_status_text = ft.Text("Supabase: verificando...")
        self.last_sync_text = ft.Text("Ultima sincronizacion: --:--:--", size=12, color=ft.Colors.GREY_400)
        self.polling_active = False
        self._chrome_onboarding_last_launch = 0.0
        self._onboarding_poll_cancel: asyncio.Event | None = None

    def log(self, message: str) -> None:
        self.log_console.value = (self.log_console.value + f"\n- {message}").strip()
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

        def open_chrome_cdp(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome instalado en la ruta esperada.")
                return
            now = time.monotonic()
            if now - self._chrome_onboarding_last_launch < 2.5:
                self.log("Chrome ya se lanzo hace pocos segundos (doble clic o evento duplicado); ignorado.")
                return
            self._chrome_onboarding_last_launch = now
            self.config = self.config_repo.load()
            cfg = self.config
            use_ephemeral = bool((cfg.get("app") or {}).get("requires_new_chrome_profile", False))
            if use_ephemeral:
                runs_root = _chrome_runs_dir_from_cfg(cfg)
                runs_root.mkdir(parents=True, exist_ok=True)
                profile_dir = runs_root / f"bl_{time.time_ns()}"
                profile_dir.mkdir(parents=False)
                user_data_arg = str(profile_dir)
                self.log(
                    f"Chrome CDP con perfil efimero (requires_new_chrome_profile): {user_data_arg}. "
                    "Inicia sesion de nuevo en esta ventana."
                )
            else:
                profile_fixed = _chrome_profile_dir_from_cfg(cfg)
                user_data_arg = str(profile_fixed)
                self.log(
                    f"Chrome CDP: perfil desde config (app.biting_lobster_chrome_profile) => {user_data_arg}. "
                    "No es el Chrome habitual del escritorio; completa autenticacion y cola aqui."
                )
            subprocess.Popen(
                [
                    str(CHROME_PATH),
                    "--remote-debugging-port=9222",
                    f"--user-data-dir={user_data_arg}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--new-window",
                    TICKETS_HOME_URL,
                ]
            )

        def open_chrome_normal(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome instalado en la ruta esperada.")
                return
            subprocess.Popen([str(CHROME_PATH), "--new-window", TICKETS_HOME_URL])
            self.log("Chrome normal: perfil por defecto del sistema (sin CDP).")

        def open_chrome_normal_fifa_com(_: ft.ControlEvent) -> None:
            if not CHROME_PATH.exists():
                self.log("No se encontro Chrome instalado en la ruta esperada.")
                return
            subprocess.Popen([str(CHROME_PATH), "--new-window", FIFA_COM_TICKETS_PAGE])
            self.log("Chrome normal: nueva ventana con fifa.com (sin CDP).")

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
                            ft.OutlinedButton("Iniciar Chrome", on_click=open_chrome_cdp),
                            ft.Button("Iniciar Chrome Normal", on_click=open_chrome_normal),
                            ft.Button(
                                "Abrir en nueva pestaña fifa.com",
                                on_click=open_chrome_normal_fifa_com,
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
                        "Deteccion automatica cada ~25 s (solo con Chrome CDP en 9222 abierto): si hay pestaña en "
                        "access.tickets.fifa.com/.../selectqueue.do con «El acceso está restringido temporalmente», "
                        "se escribe requires_new_chrome_profile=true en config.yaml.",
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
            self.log("Captura de sesion: conectando por CDP a Chrome (puerto 9222)...")
            try:

                def run_capture() -> dict[str, Any]:
                    mgr = SessionManager(session_file=str(session_path))
                    return mgr.capture_session()

                result = await asyncio.to_thread(run_capture)
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
            self.page.update()

        step2 = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Paso 2 - Usa el navegador que se abrio para iniciar tu sesion de manera normal."),
                    ft.Text(
                        "No olvides validar Captcha y cualquier codigo enviado a tu correo electronico.",
                        size=13,
                        color=ft.Colors.GREY_300,
                    ),
                    ft.Text(
                        "Cuando ya veas la tienda FIFA autenticada, pulsa capturar (Chrome con CDP debe seguir abierto).",
                        size=12,
                        color=ft.Colors.GREY_400,
                    ),
                    ft.Button(
                        "Capturar y guardar session.json (CDP)",
                        on_click=lambda e: self.page.run_task(capture_session_click),
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

        def save_onboarding(_: ft.ControlEvent) -> None:
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
            hunter_cfg = dict(self.config.get("hunter") or {})
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
                    "app": {"start_on_boot": start_on_boot},
                    "hunter": hunter_cfg,
                }
            )
            self.config = updated
            self.log("Onboarding guardado en config.yaml.")
            self.log("Cerrando Chrome CDP (puerto 9222) si estaba abierto...")
            try:
                _kill_windows_chrome_cdp_port_9222()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Aviso al cerrar Chrome CDP: {exc}")
            time.sleep(0.8)
            self.show_dashboard()

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
                    ft.Button("Guardar y abrir Dashboard", on_click=save_onboarding),
                ]
            ),
        )

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
        self._onboarding_poll_cancel = asyncio.Event()
        self.page.run_task(onboarding_cdp_poll_loop)

    def show_dashboard(self) -> None:
        if self._onboarding_poll_cancel is not None:
            self._onboarding_poll_cancel.set()
            self._onboarding_poll_cancel = None
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
                        self.log_console,
                    ],
                    expand=True,
                ),
            )
        )
        self.status_text.value = "Estado: Dashboard listo"
        self.page.update()
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


def main(page: ft.Page) -> None:
    app = DashboardApp(page)
    app.run()


if __name__ == "__main__":
    ft.run(main)
