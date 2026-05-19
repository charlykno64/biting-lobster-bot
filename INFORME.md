# Informe del proyecto — Biting Lobster Bot (Hunter FIFA)

**Documento:** resumen del esfuerzo técnico y conclusiones  
**Proyecto:** biting-lobster-bot  
**Ámbito:** tienda FIFA (México 2026) — listado de partidos, filtro por país/equipo y apertura de partido bajo protección DataDome

---

## 1. Objetivo

Automatizar de forma fiable el flujo en la tienda FIFA:

1. Entrar al listado de partidos  
2. **Filtrar por país/equipo**  
3. **Abrir un partido** y avanzar hacia la compra de entradas  

Todo ello minimizando la detección por **DataDome** (shadowban retardado, bloqueos anti-bot y pantallas de error).

---

## 2. Entorno técnico

| Componente | Descripción |
|------------|-------------|
| Automatización | Playwright (Python), opción de adjuntarse a **Chrome por CDP** |
| Sesión | `session.json` capturada desde onboarding |
| Configuración | `config.yaml` — equipos, proxy, viewport, modo probe, pausas |
| UI | Dashboard Flet (`ui/app.py`) — onboarding y ejecución del hunter |
| Mapeo de países | `core/team_mapping.py` — ID FIFA → nombre en español para teclear en el filtro |
| Motor principal | `core/HunterService.py` |

**Nota sobre el repositorio:** la mayor parte del trabajo de evasión y del probe quedó en cambios locales; el historial Git visible tiene pocos commits. Este informe describe el trabajo realizado en desarrollo, no solo lo commiteado.

---

## 3. Qué se construyó

### 3.1 Flujo general (modo probe)

Con `hunter.team_filter_probe_only: true` el bot ejecuta un recorrido acotado para pruebas:

1. Navegación al listado de partidos  
2. **Fase 1** — Filtro en `<select id="team">`  
3. Localización de la fila del partido (`target_teams`, barrido DOM y/o `p#teams_M…`)  
4. **Apertura del partido** (interacción automatizada en la fila / contenedor)  
5. **Fase 2** — En pruebas: **Punto Seguro** (pausa en consola) para observar la página del partido sin más automatización  

### 3.2 Onboarding y criterios

- El usuario elige país/equipo en el onboarding; se persiste el **ID FIFA** en `search_criteria.target_teams` (ej. Canadá: `10229225507167`).  
- Validación de criterios obligatorios antes de ejecutar el hunter (`core/hunter_prereqs.py`).  
- Referencia de opciones del `<select#team>` en `tech_spec.md`.

---

## 4. Evolución del filtro de país (Fase 1) — **éxito**

### Problema inicial

DataDome parecía reaccionar a interacciones “robóticas” en el filtro: `.fill()`, `.select_option()`, `.click()` nativo, scroll instantáneo (`scroll_into_view_if_needed`), etc.

### Enfoques descartados o superados

| Enfoque | Motivo de descarte / cambio |
|---------|----------------------------|
| `select_option()` | Detectado como inyección robótica |
| `.fill()` en inputs | Evitado por política del proyecto |
| Select2 / clases `.select2-*` | El DOM inicial no expone Select2; el control real es **`<select>` nativo** |
| Búsqueda de opción en DOM tras teclear | Menú nativo renderizado por el **sistema operativo**, no accesible en DOM |
| Lectura de etiqueta solo desde `<option>` sin diccionario | Riesgo de teclear ID numérico o elegir país equivocado (ej. primera opción visible) |

### Solución final del filtro (validada)

1. Diccionario local **`TEAM_MAPPING_ES`** (`core/team_mapping.py`) — lookup por ID, sin teclear el ID numérico.  
2. Clic humanizado (Bézier) en `#team` para foco inicial.  
3. **Type-ahead nativo:** `keyboard.type` con nombre sin acentos + delays entre teclas.  
4. **`Enter`** para confirmar la selección en el menú del SO.  
5. Pausa de **lectura** (2,5–4 s) antes de continuar hacia el partido.  

**Resultado:** el filtro de país **no dispara shadowban** de forma consistente en las pruebas realizadas.

---

## 5. Apertura del partido — **bloqueo principal**

### Síntoma

Tras filtrar correctamente, al **abrir el partido con el bot** aparece un **shadowban retardado** (~10 segundos después), visible en la página del partido o en el comportamiento del listado.

### Enfoques probados en el clic / acercamiento al partido

| # | Enfoque | Notas |
|---|---------|--------|
| 1 | `scroll_into_view_if_needed()` | Descartado — “teletransporte” en DOM |
| 2 | Scroll con `mouse.wheel` | Sustituido por scroll con **teclado** (flechas) |
| 3 | Curva **Bézier** + clic en el elemento | Sospecha de patrón matemático demasiado perfecto |
| 4 | Punto de clic descentrado (20–80 % del bbox) | Mejora menor; no eliminó el ban |
| 5 | Pausas largas post-filtro (2,5–4 s) | Reduce ritmo “inhumano”; no eliminó el ban en clic automático |
| 6 | **`focus` + `Enter`** en la fila (accesibilidad) | Sin curva Bézier en el clic final |
| 7 | Fallback: `hover` + `mouse.down` / `mouse.up` | Sin trayectoria calculada |
| 8 | Scroll previo: flechas + micro-movimiento aleatorio del ratón | Parte del flujo previo al clic |

Ninguna variante probada eliminó el shadowban retardado al **abrir el partido de forma automatizada**.

---

## 6. Pruebas de aislamiento (metodología)

Se usaron **puntos de parada** (`input` en consola) en distintas fases para acotar el momento exacto del bloqueo.

| Prueba | Resultado |
|--------|-----------|
| Entorno CDP / sesión sin acciones agresivas | El entorno en sí no explica solo el fallo |
| Parada tras filtrar país (sin clic en partido) | Sin shadowban atribuible al filtro |
| Bot filtra + **usuario abre partido manualmente** | **Sin shadowban** en la página del partido (repetido muchas veces) |
| Bot filtra + **bot abre partido** | **Shadowban retardado** |

### Conclusión de aislamiento

- **No** encaja con: sesión o proxy “quemados” de forma absoluta, o imposibilidad de ver partidos con esa cuenta.  
- **Sí** encaja con: la **apertura automatizada del partido** (o la secuencia programática inmediatamente anterior) activa el bloqueo retardado.  
- Existe un **camino humano viable** tras el filtro automático; el techo actual es **automatizar el último paso** con Playwright/CDP en las formas probadas.

---

## 7. Hallazgos principales

1. **DataDome no bloquea** el filtro de país cuando se usa el `<select>` nativo con teclado y diccionario local.  
2. El **shadowban asíncrono** se asocia a la **apertura automatizada del partido**, no al filtro en sí.  
3. **Misma sesión, mismo proxy, mismo listado filtrado:** clic manual en el partido → OK; clic del bot → bloqueo.  
4. Seguir iterando micro-optimizaciones del ratón (Bézier, rueda, etc.) mostró **rendimiento decreciente** frente al tiempo y coste.  

---

## 8. Estado por área

| Área | Estado |
|------|--------|
| Login / sesión / navegación al listado | Funcional en el stack probado |
| Filtro por país/equipo | **Validado** (enfoque humanizado) |
| Apertura automática del partido | **No viable** con las variantes probadas (Playwright/CDP) |
| Compra end-to-end automatizada | **No alcanzada** |
| Alternativa realista | **Semi-automación:** bot filtra (y puede localizar partido); **humano** abre partido y completa la compra |

---

## 9. Tipo de esfuerzo invertido

- Ingeniería de **evasión comportamental** (teclado, tiempos, evitar APIs sintéticas en puntos críticos).  
- **Depuración sistemática** con pruebas de aislamiento y puntos de parada.  
- **Refactors repetidos** de `HunterService.py` (filtro, scroll, clic, fases del probe).  
- Creación de **mapeo local** de países y alineación onboarding ↔ IDs FIFA.  
- Documentación de referencia en `tech_spec.md` y configuración en `config.yaml`.  

---

## 10. Recomendación y cierre

**No se recomienda** seguir invirtiendo recursos en automatización completa del **clic de apertura del partido** con el stack actual (Playwright sobre Chrome CDP y las técnicas ya probadas).

**Entregable con valor demostrado:**

- Flujo estable hasta **filtro de país humanizado**.  
- Evidencia documentada de la **barrera en la apertura automatizada del partido**.  
- Base de código y conocimiento para un producto **asistido** (filtro automático + pasos manuales), no un bot de compra totalmente autónomo.

### Frase resumen (para financiadores o stakeholders)

> Se logró automatizar de forma estable el filtro de país en la tienda FIFA bajo DataDome. La apertura automatizada del partido provoca un bloqueo retardado que no se reproduce con el mismo filtro y un clic humano. Por límite técnico y económico, no se justifica seguir iterando en automatización completa de ese paso con este stack; el resultado útil es el filtro asistido y la evidencia de dónde termina la viabilidad técnica.

---

## 11. Referencias en el repositorio

| Archivo | Contenido relevante |
|---------|---------------------|
| `core/HunterService.py` | Motor del hunter, probe, filtro humanizado, apertura de partido |
| `core/team_mapping.py` | `TEAM_MAPPING_ES` y resolución por idioma |
| `config.yaml` | Flags de probe, pausas, proxy, `target_teams` |
| `tech_spec.md` | HTML de referencia del `<select id="team">` |
| `ui/app.py` | Onboarding, `TEAM_OPTIONS`, ejecución del hunter |
| `PRD.md` / `tech_spec.md` | Requisitos y rutas del flujo FIFA |

---

*Documento generado para facilitar la comunicación del cierre del proyecto y la devolución o liquidación del encargo con base técnica.*
