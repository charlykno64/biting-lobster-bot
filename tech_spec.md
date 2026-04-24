# Technical Specification Document: Biting Lobster (POC)

## 1. Technical Architecture and System Design
- **Plataforma:** Desktop (Windows/macOS/Linux).
- **Lenguaje:** Python 3.11+.
- **Arquitectura Base:** Clean Architecture adaptada a scripts de automatización.
  - **Domain Layer:** Business logic pura (Entidades como Match, Ticket, UserSession), Use Cases (MonitorPrices, ReserveTicket) y abstracciones de notificación (NotifyUser).
  - **Data Layer:** Implementaciones de PlaywrightBrowser, persistencia de sesiones en JSON y logging de errores. Repositorios para persistencia local (SQLite/JSON para cookies y configuración) y PlaywrightWrapper para la interacción web.
  - **Presentation Layer:** UI reactiva usando Flet (basado en Flutter) con arquitectura MVVM. Comunicación entre capas mediante inyección de dependencias simple.
- **Concurrencia:** asyncio para el loop de monitoreo no bloqueante de la UI.

## 2. Data Schemas and State Management
- **Manejo de Moneda (Crucial):** Patrón Zero-Decimal. Todos los precios se procesan como Int (centavos de USD). La conversión a formato moneda ($XX.XX) es responsabilidad exclusiva de los formatters de la UI.
- **Esquemas Base (Ejemplo Domain Models):**
  - UserSession: session_id (String), storage_state (Dict/Path), is_premium (Boolean).
  - MatchCriteria: team_name (String), max_price_cents (Int), quantity (Int).
- **Almacenamiento Local:** - session.json: Almacena el storage_state de Playwright (cookies, local storage).
  - Esquema de persistencia local: {
  "auth": {
    "session_file": "storage_state.json",
    "last_login": "2026-04-23T..."
  },
  "criteria": {
    "target_teams": ["Mexico", "USA"],
    "max_price_cents": 20000,
    "ticket_limit": 1
  },
  "counters": {
    "tickets_secured": 0,
    "is_premium": false
  }
}
  - config.yaml: Preferencias de usuario, tokens de Telegram y límites de búsqueda.

## 3. UI/UX Specifications and Navigation
- **UI Framework:** Flet (Material Design 3 nativo).
- **Sistema de Diseño:** 
  - Color: primary: #006BB6 (FIFA Blue), secondary: #ED1C24 (Red).
  - Dark Mode soportado por defecto para reducir fatiga visual durante monitoreos largos.
- **Componentes Core:** LogConsole (Scrollable para ver el estatus en tiempo real), MatchCard (Muestra partido encontrado y botón de acción).
- **Navegación:** Router interno de Flet manejando estados: Splash -> Setup/Login -> Dashboard (Hunter Mode).

## 4. Business Logic and Algorithms
- **Estrategia de Cacería (Hunter Algorithm):**
  1. Interceptación XHR: Playwright intercepta las respuestas JSON del API de la FIFA para detectar cambios de disponibilidad sin re-renderizar el DOM.
  2. Jitter Dinámico: Intervalos de refresco basados en una función aleatoria gaussiana para evitar patrones detectables por WAF (Web Application Firewalls).
  3. Auto-Carting: Al detectar disponibilidad, el script ejecuta un click simulado con coordenadas aleatorias dentro del botón "Add to Cart" para humanizar la interacción.
  4. URLS relevantes:
    - URL inicial: https://fwc26-shop-mex.tickets.fifa.com/secured/content en esta se debe hacer click en el botón "Comprar boletos" ejemplo: <a class="sc-TOsTZ FeKdn sc-gqjmRU g-Button g-Button-small g-Button-primary gaYhxh" href="https://fwc26-shop-mex.tickets.fifa.com/secured/selection/event/date?productId=10229225515651&amp;gtmStepTracking=true" aria-label="COMPRAR BOLETOS Copa Mundial de la FIFA 2026™"><span>COMPRAR BOLETOS</span></a>
    - Paises: <select id="team" autocomplete="off">
							<option value="">Cualquier equipo</option>
							<option value="11404606582">Alemania</option>
							<option value="11404606664">Arabia Saudí</option>
							<option value="11404606510">Argelia</option>							
							<option value="11404606516">Argentina</option>
						  <option value="11404606519">Australia</option>
	            <option value="11404606520">Austria</option>
              <option value="11404606533">Bosnia y Herzegovina</option>
							<option value="11404606535">Brasil</option>
							<option value="11404606527">Bélgica</option>
							<option value="11404606541">Cabo Verde</option>
							<option value="10229225507167">Canadá</option>
							<option value="11404606656">Catar</option>b
							<option value="11404606550">Colombia</option>
							<option value="11404606504">Costa de Marfil</option>
							<option value="11404606556">Croacia</option>
							<option value="11404606558">Curasao</option>
							<option value="11404606565">Ecuador</option>
							<option value="10229225507169">EEUU</option>
							<option value="11404606566">Egipto</option>
							<option value="11404606665">Escocia</option>
							<option value="11404606677">España</option>
							<option value="11404606577">Francia</option>
							<option value="11404606583">Ghana</option>
							<option value="11404606592">Haití</option>
							<option value="11404606568">Inglaterra</option>
							<option value="11404606600">Irak</option>
							<option value="11404606604">Japón</option>
							<option value="11404606605">Jordania</option>
							<option value="11404606634">Marruecos</option>
							<option value="10229225507168">México</option>
							<option value="11404606646">Noruega</option>
							<option value="11404606641">Nueva Zelanda</option>
							<option value="11404606650">Panamá</option>
							<option value="11404606652">Paraguay</option>
							<option value="11404606639">Países Bajos</option>
							<option value="11404606654">Portugal</option>
							<option value="11404606553">RD del Congo</option>
							<option value="11404606560">República Checa</option>
							<option value="11404606609">República de Corea</option>
							<option value="11404606599">RI de Irán</option>
							<option value="11404606666">Senegal</option>
							<option value="11404606675">Sudáfrica</option>
							<option value="11404606684">Suecia</option>
							<option value="11404606685">Suiza</option>
							<option value="11404606696">Turquía</option>
							<option value="11404606695">Túnez</option>
							<option value="11404606702">Uruguay</option>
							<option value="11404606704">Uzbekistán</option>
						</select>
    - En la lista de partidas podrían encontrase las siguientes leyendas: "Disponibilidad limitada", "No disponible" o simplemente no tener leyenda lo que significa que tiene disponibilidad normal.
    - Cuando se selecciona un partido se redirigirá a una pagina como esta: https://fwc26-shop-mex.tickets.fifa.com/secure/selection/event/seat/performance/10229226700888/lang/es aqui hay que seleccionar la opción "Reservar el mejor sitio".
    - Cuando aparezcan las categorias de los lugares del asiento elegir en orden de arriba a abajo, es decir por default seleccionar la Categoria 1 y si esta no esta disponible entonces la Categoria 2 y asi sucesivamente. En cantidad seleccionar 1 para garantizar que se agregue al carrito con exito rapidamente.
    - El botón "Añadir al carrito" tiene esta apariencia: <a id="book" onclick="functions.validateQuantities(true, null, $(this));return false;" href="#" role="button" aria-disabled=""><span class="icon"></span><span class="text">Añadir al carrito</span><span class="accessibility-visually-hidden">&nbsp;</span></a>
- **Formateo de UI:** Conversión de Precios: Funciones de utilidad en core/utils/ para parsear Strings de la web de FIFA (ej. "USD 40.00") a Int (4000).

## 5. Error Handling and Edge Cases
- **Baneos de IP/Shadowban:** 
  - *Edge Case:* La página devuelve 403 o pide Captcha constante.
  - *Solución:* El HunterService pausa el monitoreo, emite un ErrorState a la UI y notifica vía Telegram para que el usuario resuelva el Captcha manualmente en el navegador asistido.
- **Sesión Expirada:**
  - *Solución:* El interceptor de red detecta redirecciones al Login, detiene el loop y fuerza el estado "Auth Required" en el Dashboard.
- **Conectividad:** Reintento exponencial (Exponential Backoff) para micro-cortes de internet sin cerrar la aplicación.

## 6. Performance Requirements and Constraints
- **Consumo de Recursos:** El ejecutable final debe pesar < 80MB (usando Nuitka con zstandard y onefile mode).
- **Headless Optimization:** El monitoreo se realiza en modo headless=True para ahorrar CPU/RAM, abriendo el navegador visible (headless=False) solo cuando se requiere intervención humana o el checkout final.
- **Latencia de Notificación:** Objetivo < 2 segundos desde la detección del JSON hasta el envío del mensaje via Telegram Bot API.

## 7. Security and Compliance Considerations
- **Privacidad Local:** No se almacenan contraseñas. Solo se persiste el storage_state (cookies de sesión) generado por el usuario en su propio navegador.
- **Transparencia Legal:** Pantalla de "Disclaimer" obligatoria en el primer inicio detallando que la app automatiza acciones que el usuario podría hacer manualmente y no vulnera la infraestructura de la FIFA.
- **Tráfico Seguro:** Todas las comunicaciones con el API de Telegram y FIFA se realizan forzosamente sobre TLS/HTTPS.

## 8. Testing Approach and Quality Criteria
- **Unit Testing (Domain):** Pruebas de los algoritmos de filtrado de partidos y cálculo de precios usando pytest.
- **Integration Testing:** Mock de respuestas de red para asegurar que el HunterService dispara la acción de "Agregar al Carrito" correctamente ante un JSON positivo.
- **UI Previews:** Implementación de modo debug en la UI para visualizar estados de "Boleto Encontrado" sin necesidad de conexión real.

## 9. AI Agent Implementation Plan (Prompts by Epic & User Story & Tasks)
- **Epic 1: Infraestructura Base y Captura de Sesión (Auth)**
  - Historias de Usuario cubiertas:
    - US1: Como usuario, quiero sentirme seguro al proporcionar mi usuario y contraseña, y saber que solo será usada de manera local.
    - US2: Como usuario, quiero que la aplicación inicie sesión por mi, asistiéndome solo en caso de requerir captcha.
  - Prompt para el Agente (Fase 1 - Setup & Session Manager):
"Actúa como Senior Python Engineer. Construye 'Biting Lobster' usando Clean Architecture: 
    - [] Crea entorno e instala flet, playwright, playwright-stealth, python-dotenv. Estructura: /core, /ui, /data, /domain.
    - [] Implementa SessionManager.py en /data. Usa Playwright para abrir Chrome visible (headless=False).
    - [] Navega a la URL de tickets de FIFA. El script debe esperar a que el usuario haga login manualmente (resolviendo captchas si los hay).
    - [] Una vez detectado el login exitoso, guarda el storage_state estrictamente en un archivo local session.json. NUNCA pidas ni guardes credenciales en variables.
- **Epic 2: Configuración, UI y Sistema (Dashboard)**
  - Historias de Usuario cubiertas:
    - US3: Como usuario, quiero que la aplicación me pregunte en qué equipos estoy interesado y recordar esta decisión.
    - US7: Como usuario, quiero poder consultar qué está haciendo la app (estatus) y ser notificado de problemas.
    - US9: Como usuario, quiero que la aplicación inicie automáticamente al iniciar el sistema operativo.
  - Prompt para el Agente (Fase 2 - UI & Settings):
"Implementa la capa de UI con Flet (Material 3).
    - [] Crea ConfigRepository.py para guardar selecciones en config.yaml local.
    - [] Crea la vista de Configuración: Dropdown de equipos (usa IDs del HTML de FIFA), Límite de precio y Token de Telegram.
    - [] Crea el Dashboard Principal: Añade un componente LogConsole (scrollable) para mostrar el estatus de la app en tiempo real.
    - [] Implementa una función utilitaria para Windows (modificando el registro de HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run o carpeta Startup) que permita arrancar el .exe al encender la PC (opcional por toggle)."
- **Epic 3: Motor de Cacería (Hunter Algorithm)**
  - Historias de Usuario cubiertas:
    - US3: Como usuario, quiero que la aplicación me pregunte en qué equipos estoy interesado y recordar esta decisión.
    - US7: Como usuario, quiero poder consultar qué está haciendo la app (estatus) y ser notificado de problemas.
    - US9: Como usuario, quiero que la aplicación inicie automáticamente al iniciar el sistema operativo.
  - Prompt para el Agente (Fase 2 - UI & Settings): "Implementa la capa de UI con Flet (Material 3).
    - [] Inicia Playwright con stealth y carga session.json. Usa headless=True.
    - [] Navega a la URL de la tienda. Implementa interceptación XHR para leer las respuestas JSON de disponibilidad.
    - [] Usa un 'Jitter' (retraso aleatorio gaussiano) entre peticiones para evitar baneos.
    - [] Al encontrar disponibilidad, navega al detalle del asiento. Selecciona 'Reservar el mejor sitio', intenta Categoría 1, cantidad 1.
    - [] Busca el botón con id book y haz un clic simulado con coordenadas humanizadas.
- **Epic 4: Notificaciones y Handoff (Checkout)**
  - Historias de Usuario cubiertas:
    - US5: Como usuario, quiero que la app me notifique en Telegram y sistema cuando agregue un boleto, dando acceso al carrito.
    - US6: Como usuario, quiero que al ir a revisar el carrito, la app me entregue el control para concluir la compra por mi cuenta.
  - Prompt para el Agente (Fase 4 - Notifiers & Handoff): "Implementa el sistema de notificaciones y el paso de control.
    - [] Crea TelegramNotifier.py y SystemTrayNotifier.py (usa plyer para notificaciones nativas de OS).
    - [] Cuando HunterService confirme el clic de añadir al carrito, dispara la notificación indicando equipo, precio en formato $XX.XX (conviertiendo de centavos) y urgencia.
    - [] Handoff: Inmediatamente después de agregar al carrito, detén el monitoreo. Pasa el contexto de Playwright a headless=False (haz la ventana visible) para que el usuario tome el mouse, ingrese sus datos bancarios y pague de forma segura.
- **Epic 5: Telemetría, Control de Errores y Monetización**
  - Historias de Usuario cubiertas:
    - US8: Como usuario, quiero poder agradecer con un donativo para conseguir más boletos.
    - US10: Como desarrollador, quiero que la app reporte incidencias remotas.
    - US11: Como desarrollador, quiero que registre información analítica básica.
  - Prompt para el Agente (Fase 5 - Resiliencia y Analytics): "Finaliza los requerimientos de seguridad, métricas y negocio.
    - [] Integra Sentry-sdk para reporte de incidencias remotas (errores silenciosos) y telemetría muy básica (solo eventos como 'Instalación', 'Cacería Iniciada', 'Boleto Encontrado' para respetar privacidad).
    - [] Si HunterService detecta un HTTP 403 o Captcha, detén el proceso, notifica en la consola de la UI y pide resolución manual.
    - [] Lógica de Monetización: Implementa un contador local en config.yaml. Si tickets_secured == 1, muestra un Dialog en Flet ofreciendo links de 'BuyMeACoffee' o pago en Crypto. Hasta que el backend no valide el pago (o el usuario ingrese un código de desbloqueo), bloquea nuevos intentos de cacería indicando el límite alcanzado.  