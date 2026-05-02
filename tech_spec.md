# Technical Specification Document: Biting Lobster (POC)

## 1. Technical Architecture and System Design
- **Plataforma:** Desktop (Windows/macOS/Linux).
- **Lenguaje:** Python 3.11+.
- **Arquitectura Base:** Clean Architecture adaptada a asistente manual + automatización controlada.
  - **Domain Layer:** Business logic pura (Entidades como Match, Ticket, UserSession), Use Cases (MonitorPrices, ReserveTicket) y abstracciones de notificación (NotifyUser).
  - **Data Layer:** Captura de sesión (Epic 1) con Playwright vía **CDP** contra Google Chrome abierto manualmente por el usuario; se persiste `storage_state` en `session.json`. El **HunterService (Epic 3) no usa CDP**: arranca una **nueva** instancia de Playwright **Chromium** con `headless=True`, **stealth** y `storage_state` cargado desde ese `session.json`, para interceptar XHR/JSON de forma silenciosa. Repositorios para persistencia local y logging de errores.
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
  - config.yaml: Preferencias de usuario y límites de búsqueda. Secretos (Supabase, Telegram) solo en '.env'; cargar con python-dotenv.
  - **Chrome CDP (onboarding / captura, `app` en config.yaml):**
    - `biting_lobster_chrome_profile`: ruta absoluta de `--user-data-dir` para «Iniciar Chrome» (CDP, puerto 9222); por defecto `C:\BitingLobsterChromeProfile`.
    - `chrome_profile_runs_root`: directorio padre para perfiles efímeros cuando `requires_new_chrome_profile` es `true`.
    - `requires_new_chrome_profile`: si es `true`, cada arranque CDP usa una subcarpeta nueva bajo `chrome_profile_runs_root`; el sondeo en onboarding puede ponerla en `true` si detecta la cola PKP (`access.tickets.fifa.com/.../selectqueue.do`) con texto de acceso restringido.
    - «Limpiar y usar nuevo perfil en Chrome»: termina procesos Chrome con `remote-debugging-port=9222`, borra y recrea la carpeta de `biting_lobster_chrome_profile` y deja `requires_new_chrome_profile` en `false`.
  - **Velocidad del Hunter (jitter):** en `config.yaml`, clave `hunter.speed` con valores `alta` | `media` | `baja` (por defecto **`baja`** en desarrollo). Cada valor define un rango de **retraso aleatorio uniforme entre pasos** (en segundos): `alta` 0.200–0.399 s, `media` 0.400–0.799 s, `baja` 0.800–1.200 s. Objetivo: no saturar el origen; la UI puede exponer el mismo control más adelante.
  Nota técnica: Gestión de variables de entorno (v1.0.0): Durante la primera versión, la aplicación utilizará exclusivamente el archivo local .env.dev para cargar secretos y parámetros sensibles (por ejemplo, SUPABASE_URL, SUPABASE_KEY, TELEGRAM_BOT_TOKEN).
  El archivo .env.dev es de uso local, no debe versionarse y debe estar excluido en .gitignore.
  El archivo .env.example se incorporará a partir de la versión v1.0.1 como plantilla sin secretos para estandarizar despliegue y onboarding técnico.
  - Sincronización de licencias y modos de access_granted: La tabla remota licenses (Supabase) es la fuente de verdad para hardware_id y access_granted. Modo automatizado: BMC / PayPal abiertos desde la UI; el usuario incluye hardware_id en notas; integraciones externas (p. ej. Zapier) actualizan access_granted (p. ej. a DONATED) cuando el proveedor de pago entrega los metadatos necesarios.
  Modo manual / fuera de banda: un operador o proceso no ligado a la app actualiza la misma tabla; el cliente solo relee el estado.
  No existe estado intermedio PENDING en el producto: la UI no ofrece un tercer flujo de “donación explícita”; el usuario sigue las instrucciones y la app refresca el registro según los criterios de temporización definidos en 10 y luego 30 segundos siempre (enfoco de ventana) sucesivamente.
  - Esquema de la base de datos en Supabase:
  ## Table `licenses`

  ### Columns

  | Name | Type | Constraints |
  |------|------|-------------|
  | `id` | `int8` | Primary Identity |
  | `created_at` | `timestamptz` |  |
  | `hardware_id` | `varchar` |  Nullable Unique |
  | `access_granted` | `varchar` |  Nullable |
  | `supporter_name` | `varchar` |  Nullable |
  | `supporter_email` | `varchar` |  Nullable |
  | `telegram_chat_id` | `int8` |  Nullable |



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
  2. Jitter dinámico: intervalos de refresco entre pasos con **retraso aleatorio uniforme** dentro de un rango definido por `hunter.speed` en `config.yaml` (`alta` / `media` / `baja`; por defecto `baja` en desarrollo) para moderar la presión sobre el origen y reducir patrones rígidos.
  3. Auto-Carting: Al detectar disponibilidad, el script ejecuta un click simulado con coordenadas aleatorias dentro del botón "Add to Cart" para humanizar la interacción.
  4. URLS relevantes:
    - URL inicial: `https://fwc26-shop-mex.tickets.fifa.com/secured/content`; flujo natural con clic en **Comprar boletos** hacia selección de fechas. FIFA puede servir enlaces con `/secure/` o `/secured/` y `selection/event/date` en el `href` (con `productId` en query o `/product/<id>/` en path).
    - **Headless / DOM:** el primer `<a>` que coincida por texto puede ser un `role="menuitem"` oculto hacia `/secured/content` (menú). El Hunter prioriza enlaces **visibles** cuyo `href` indique pantalla de fechas; si no hay CTA clicable, hace **`goto`** a la URL canónica del listado (`match_list_url`) y registra el motivo en log.
    - **Criterio obligatorio:** `search_criteria.target_teams` no puede estar vacío; si el onboarding se guardó sin equipo (o la lista se borró en YAML), `run_hunter` falla de forma explícita antes de elegir fila en `li.performance`.
    - Ejemplo histórico de CTA (referencia): botón "Comprar boletos" con `href` a `.../secured/selection/event/date?productId=...` y clases tipo `g-Button-primary`.
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
- **Restricción temporal FIFA (cola / anti-bot):** mensajes tipo «acceso restringido» en `access.tickets.fifa.com` pueden depender de perfil, IP, ritmo y sesiones paralelas; borrar y recrear la carpeta del perfil CDP dedicado ha mostrado alivio en pruebas, **sin garantía** de solución definitiva ni de duración del bloqueo.
- **Baneos de IP/Shadowban:** 
  - *Edge Case:* La página devuelve 403 o pide Captcha constante.
  - *Solución:* El HunterService pausa el monitoreo, emite un ErrorState a la UI y notifica vía Telegram para que el usuario resuelva el Captcha manualmente en el navegador asistido.
- **Sesión Expirada:**
  - *Solución:* El interceptor de red detecta redirecciones al Login, detiene el loop y fuerza el estado "Auth Required" en el Dashboard.
- **Conectividad:** Reintento exponencial (Exponential Backoff) para micro-cortes de internet sin cerrar la aplicación.

## 6. Performance Requirements and Constraints
- **Consumo de Recursos:** El ejecutable final debe pesar < 200MB.
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
    - [] Crea entorno e instala flet, playwright, python-dotenv. Guarda en un script llamado "setup-windows" para recrear el entorno en caso de ser necesario en Windows.
    - [] Estructura de carpetas: /core, /ui, /data, /domain. 
    - [] Implementa SessionManager.py en /data. NO debe lanzar un navegador nuevo. Debe conectarse via CDP a una instancia de Google Chrome abierta manualmente por el usuario.
    - [] El modo por defecto debe ser asistido/manual y legal: sin logica de evasión o bypass de controles de seguridad.
    - [] Conecta Playwright usando chromium.connect_over_cdp("http://127.0.0.1:9222") a la sesion iniciada por el usuario.
    - [] Una vez detectado el login exitoso, guarda el storage_state estrictamente en un archivo local session.json. NUNCA pidas ni guardes contraseñas en variables.
    - [] Valida el usuario mediante su perfil en https://fwc26-shop-mex.tickets.fifa.com/account/editPersonalDetails ya que se deben cumplir dos reglas de negocio:
    	- Límite por hogar: es de cuatro (4) por partido. Todas las compras vinculadas a la misma dirección registrada en la cuenta FIFA se contabilizan para estos límites.
     	- Restricción diaria: Solo puedes solicitar o comprar boletos para un partido por día."
- **Epic 2: Configuración, UI y Sistema (Dashboard)**
  - Historias de Usuario cubiertas:
    - US3: Como usuario, quiero que la aplicación me pregunte en qué equipos estoy interesado y recordar esta decisión.
    - US7: Como usuario, quiero poder consultar qué está haciendo la app (estatus) y ser notificado de problemas.
    - US9: Como usuario, quiero que la aplicación inicie automáticamente al iniciar el sistema operativo.
  - Prompt para el Agente (Fase 2 - UI & Settings):
"Implementa la capa de UI con Flet (Material 3).
    - [] Crea ConfigRepository.py para guardar selecciones en config.yaml local.
    - [] Implementa un chequeo Geográfico inicial usando ip-api.com. Si no es Mexico, o United States o Canada, muestra un error y bloquea la app.
    - [] Crea la vista OnboardingWizard: Pantalla 1 (Aviso de Privacidad y Disclaimer legal explícito), Pantalla 2 (Login/Captura de Sesión), Pantalla 3 (Selección de País Dropdown de equipos (usa IDs del HTML de FIFA), Categorias preferidas y Límite de precio).
    - [] Las categorias distintas categorias pero en general se pueden clasificar como:
      - Categoria 1, Categoria 2, Categoria 3 y Categoria 4.
      - Algunas Categorias como la 1 se pueden llegar a subdividir, por ejemplo: Zona delantera (categoría 1) pero agrupalas y consideralas simplemente como su categoria padre en este caso "Zona delantera (categoría 1)" se debe tratar co Categoría 1.
      - Las preferencias por Categorias tiene un orden de importancia, si se elige primero la Categoria 3 y luego la Categoria 2 se debe priorizar la selección de la Categoria 3 sobre la 2. Si solo hay un boleto disponible para la Categoria 3 se selecciona y se agrega al carrito y se completa con otras categorias según la prioridad de categorias hasta completar el valor de quantity en config.yaml. Si quantity no se logra completar por falta de disponibilidad entonces se procede a continuar el flujo de manera normal.
    - [] El limite de precio es un campo de texto en USD dolares que permite excluir aquellos boletos que superan el precio limite. Dependiendo de la cuenta, el precio puede aparecer en MXN o USD o Dolares Canadienses y se tiene que hacer la conversión según corresponda para filtrar correctamente. La información para la conversión se encuentra en config.yaml en la entrada currency_rates donde se encuentran los valores para convertir las monedas USD, MXN y CAD.
    - [] Crea el Dashboard Principal con un componente LogConsole (scrollable) y la opción de Iniciar con el sistema.
    - Muestra en la configuración el valor de ID único de hardware de manera que el usuario lo pueda visualizar y copiar.
    - [] Implementa un chequeo a un endpoint de control remoto en Supabase usando un ID único de hardware. Si el ID no existe entonces lo inserta con hardware_id = <ID único de hardware>, access_granted = 'LIMITED' y tickets_secured = 0, y si ya existia previamente valida si access_granted = 'FULL' entonces permite usar la aplicación completamente y max_tickets_secured = 40 y desactiva las opciones de monetización. Si access_granted = 'LIMITED' entonces mantiene habilitadas las opciones de monetización y permite usar la aplicación hasta que logre agregar 1 boleto a su carrito (tickets_secured == 1). El estado debe ser guardado en Supabase y no debe modificarse localmente. Solo debe actualizarse el estado local de la aplicación leyendo el estado desde Supabase. La estrategia de polling es de 30 segundos. Usa el ID único de hardware para identificar al usuario."
- **Epic 3: Motor de Cacería (Hunter Algorithm)**
  - Historias de Usuario cubiertas:
    - US3: Como usuario, quiero que la aplicación me pregunte en qué equipos estoy interesado y recordar esta decisión.
    - US7: Como usuario, quiero poder consultar qué está haciendo la app (estatus) y ser notificado de problemas.
    - US9: Como usuario, quiero que la aplicación inicie automáticamente al iniciar el sistema operativo.
  - Prompt para el Agente (Fase 3 - Hunter Service):
"Implementa HunterService.py en /core usando asyncio.
    - [] Implementa HunterService.py en /core usando asyncio.
    - [] **No usar CDP** en el Hunter: el `session.json` ya fue generado y validado en Epic 1 vía CDP contra Chrome del usuario. Aquí se inicia **nueva** instancia Playwright Chromium con **stealth** + **headless=True** y `browser.new_context(storage_state=... session.json)`. Documentar en UI/onboarding que **conviene cerrar Google Chrome** usado para la captura antes de iniciar la cacería (menos confusión, menos RAM, evita percepción de dos sesiones competidoras).
    - [] Navega a la URL de la tienda. Implementa interceptación de respuestas XHR/fetch JSON del host FIFA para inferir disponibilidad sin depender solo del DOM.
    - [] **Jitter** configurable en `config.yaml` → `hunter.speed`: `alta` (retraso aleatorio uniforme 200–399 ms entre pasos), `media` (400–799 ms), `baja` (800–1200 ms). **Default recomendado: `baja`** (desarrollo y primera implementación poco agresiva).
    - [] Al encontrar disponibilidad, navega al detalle del asiento. Selecciona 'Reservar el mejor sitio' y agrega quantity según disponibilidad y la prioridad de Categorias en config.yaml.
    - [] Al leer el precio del JSON, pásalo por CurrencyConverter (tasas en config.yaml) y conviértelo a centavos de USD antes de compararlo con max_price_cents. Si el precio convertido es menor o igual al límite, procede al Auto-Carting.
    - [] Busca el botón con id="book" y haz un clic simulado con coordenadas humanizadas y emite evento de Notificación de 'Boleto Asegurado'."
- **Epic 4: Notificaciones y Handoff (Checkout)**
  - Historias de Usuario cubiertas:
    - US5: Como usuario, quiero que la app me notifique en Telegram y sistema cuando agregue un boleto, dando acceso al carrito.
    - US6: Como usuario, quiero que al ir a revisar el carrito, la app me entregue el control para concluir la compra por mi cuenta.
  - Prompt para el Agente (Fase 4 - Notifiers & Handoff): "Implementa el sistema de notificaciones y el paso de control.
    - [] Crea TelegramNotifier.py y SystemTrayNotifier.py (usa plyer para notificaciones nativas de OS).
    - [] Cuando HunterService confirme el clic de añadir al carrito, dispara la notificación indicando equipo, precio en formato $XX.XX (conviertiendo de centavos) y urgencia (haz la ventana visible).
    Nota técnica: 
      - Auto-Carting exitoso: El script en headless=True logra hacer clic en "Añadir al carrito".
      - Estado: El script inmediatamente guarda el storage_state actualizado (que ahora contiene la cookie del carrito activo) en nuestro archivo session.json.
      - Cerrar y Reabrir: El script cierra la instancia oculta y lanza inmediatamente una nueva instancia de Playwright, pero esta vez con headless=False y cargando el session.json.
      - Redirección: Esta nueva ventana visible navega directamente a la URL del carrito /checkout.
      - Control total: ¡Listo! El usuario ve la ventana emergente con su sesión activa y su boleto esperando el pago.
    - [] Usa la URL https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates donde obtendrás un JSON asi: {"ok":true,"result":[{"update_id":32280098,
    "message":{"message_id":2,"from":{"id":8786874544,"is_bot":false,"first_name":"Carlos","last_name":"Cano","language_code":"es"},"chat":{"id":8786874544,"first_name":"Carlos","last_name":"Cano","type":"private"},"date":1777332561,"text":"123456789"}}]}
    Aqui hay dos datos importantes: 1) El valor de la llave "text" (en este ejemplo 123456789) y 2) su correspondiente "id" de chat (en este ejemplo 8786874544). Con estos dos valores debes primero validar si alguna de los hardware_id corresponde al mismo de la aplicación, luego si encontraste la conincidencia debes ir a supabase, buscar en la tabla de licenses por hardware_id = text y si lo encuentra entonces actualizar el campo telegram_chat_id = id.
    Nota Técnica: Al invocar getUpdates, hazlo ESTRICTAMENTE sin el parámetro offset. Esto devolverá el histórico de los últimos mensajes. La app debe filtrar este array buscando text == hardware_id_local. Nunca marques el mensaje como leído (evita usar offset) para no afectar a otras instancias del bot. Estrategia temporal de sincronización Telegram para reducir complejidad en el MVP y dado el bajo volumen inicial (2 a 3 usuarios), la integración de Telegram consumirá getUpdates sin parámetro offset, filtrando localmente por hardware_id para asociar telegram_chat_id. Esta decisión se considera temporal y controlada por alcance.
    - [] Una vez que se registra exitosamente el telegram_chat_id se debe de enviar un mensaje confirmando la integración invocando: https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage?chat_id={ID_CHAT}&text={MENSAJE_CONFIRMACION} donde el mensaje de confirmaciónm es una respuesta para que el usuario sepa que ya tiene activado la comunicación con Telegram y el bot.
    - [] Handoff: Inmediatamente después de agregar al carrito, detén el monitoreo. Pasa el contexto de Playwright a headless=False para que el usuario tome el mouse, ingrese sus datos bancarios y pague de forma segura.
    - [] Actualiza el contador tickets_secured en config.yaml y persiste el cambio."
- **Epic 5: Negocio y Monetización**
  - Historias de Usuario cubiertas:
    - US8: Como usuario, quiero poder agradecer con un donativo para conseguir más boletos.
  - Prompt para el Agente (Fase 5 - Negocio y Monetización): "Finaliza los requerimientos de negocio y monetización en la UI.
    - [] Si tickets_secured == 1 es igual a max_tickets_secured (por default = 1), muestra un Paywall en la UI con opciones de pago nativas (enlaces a Buy Me a Coffee y PayPal). Al tocar cualquiera de estas opciones se debe de mostrar un mensaje al usuario que para instruirle que pegue el valor de hardware id en la sección de notas (no hay que decirle que es el valor sino que es necesario agregarlo para hacer la operación de manera exitosa).
    - [] Si access_granted = "DONATED" entonces max_tickets_secured debe ser igual a 2.
    - [] Limita estrictamente: Si tickets_secured >= 40, bloquea permanentemente la app para ese usuario mostrando mensaje de límite alcanzado. Permitir borrar cuenta local. Bloquea nuevos intentos de cacería indicando el límite alcanzado.  
    - [] Si HunterService detecta un HTTP 403 o Captcha, detén el proceso, notifica en la consola de la UI y pide resolución manual.
    - [] Lógica de Monetización: Implementa un contador local en config.yaml. Si max_tickets_secured = tickets_secured, muestra un Dialog en Flet ofreciendo links de 'BuyMeACoffee' y Paypal y bloquea nuevos intentos de cacería indicando el límite alcanzado. Hasta que Supabase confirme access_granted = DONATED el el limite se incrementará (max_tickets_secured).        
- **Epic 6: Telemetría, Control de Errores y Pruebas Unitarias**
  Cubriendo: Reporte de incidencias (US10), Analítica (US11), Pruebas Unitarias.
  - Prompt para el Agente (Fase 6 - Observability & Tests):
"Implementa la telemetría y pruebas (NFRs).
    - [] Integra Sentry-sdk para reporte de incidencias remotas (errores silenciosos) y telemetría muy básica (solo eventos como 'Instalación', 'Cacería Iniciada', 'Boleto Encontrado' para respetar privacidad).
    - [] Usa loguru para crear un log local estructurado (ej. lobster.log con rotación a los 10MB) para depuración del usuario. 
    - [] Crea la suite de pruebas con pytest: Escribe tests unitarios para los conversores de moneda (Zero-Decimal) y mocks de la intercepción de red del HunterService.

- **Epic 7: Empaquetado y Distribución**
  Cubriendo: Ligereza < 200MB, Fácil instalación/desinstalación, Despliegue en MacOS.
  Prioridad de plataforma por versión:
      v1.0.0 (MVP): objetivo principal y único compromiso de entrega en macOS (ejecutable .app + instalador .dmg).
      Windows: se considera nice-to-have en esta etapa y no bloquea la liberación de v1.0.0.
      Post-MVP (v1.0.1+): se prioriza la incorporación de empaquetado e instalador para Windows.
  - Prompt para el Agente (Fase 7 - Build & Windows Installer nice2have para la versión 1.0.1):
"Crea los scripts de construcción final.
    - [] Genera un script build.py usando Nuitka (con los flags --standalone, --onefile, y --plugin-enable=flet) para compilar un ejecutable ligero (< 200MB) para MacOS usando zstandard y onefile mode.
    - El script debe invocar playwright install chromium silenciosamente post-instalación usando el siguiente comando: playwright install chromium --with-deps para mantener el paquete ligero de la aplicación.
    - [] Genera un archivo .iss (Inno Setup) para crear el instalador de Windows, asegurando que incluya atajos en el escritorio y un desinstalador limpio que borre el session.json."
  - Prompt para el Agente (Fase 7 - Build & DMG Installer):
    "Crea los scripts de construcción final para macOS.
    - [] Modifica la lógica de rutas en core/utils/ para que en macOS el session.json y config.yaml se guarden en ~/Library/Application Support/BitingLobster/.
    - [] Genera un script build_mac.py que use Nuitka o PyInstaller con el flag --windowed para crear un Mac App Bundle (.app).
    - [] Asegura que el icono del proyecto (.icns) esté correctamente vinculado en el Info.plist.
    - [] Utiliza la herramienta create-dmg para generar un archivo .dmg profesional. El DMG debe tener un fondo personalizado (si está disponible), el icono de la app y un acceso directo a la carpeta /Applications para la instalación por arrastre.
    - [] Documenta el comando codesign necesario para evitar que Gatekeeper bloquee la app (aunque sea con firma 'ad-hoc' para desarrollo)."