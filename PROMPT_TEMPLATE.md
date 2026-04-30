Actúa como Senior Python Engineer y ejecuta EXCLUSIVAMENTE la fase indicada de `tech_spec.md` (Sección 9), respetando `AI_RULES.md`.
FASE OBJETIVO:
- Ejecuta: Epic 2 "Configuración, UI y Sistema (Dashboard)"
REGLAS DE EJECUCIÓN:
1) Fuente de verdad única: `tech_spec.md` Sección 9.
2) NO implementes nada fuera de la fase objetivo.
3) Si detectas mejora, riesgo u omisión:
   - NO la implementes directamente.
   - Primero propón el cambio y actualiza `tech_spec.md` (solo si lo apruebo).
   - Después continúas implementación.
4) Mantén cambios mínimos, claros y trazables.
5) No expongas secretos en código/documentación (`.env.dev` para local, sin hardcode).
PROCESO OBLIGATORIO:
A) PRE-CHECK (antes de codificar):
- Resume alcance de la fase en 5-10 bullets.
- Lista dependencias de entrada y archivos potenciales a tocar.
- Señala riesgos de ejecución de esta fase.
- Espera confirmación breve si hay ambigüedad crítica; si no, procede.
B) IMPLEMENTACIÓN:
- Ejecuta solo tareas de la fase.
- Si una tarea depende de otra, respeta orden.
- Evita refactors no solicitados.
C) VALIDACIÓN:
- Corre validaciones mínimas razonables para esta fase (lint/tests/run parcial si aplica).
- Reporta resultados concretos: OK / warnings / pendientes.
D) CIERRE DE FASE:
- Entrega:
  1. Resumen de cambios
  2. Archivos modificados/creados
  3. Evidencia de validación
  4. Riesgos pendientes
  5. Siguiente paso recomendado (siguiente fase)
- Actualiza `PROGRESS.md` siguiendo la estructura de 'PROGRESS_TEMPLATE.md'.
CRITERIO DE ÉXITO:
- La fase queda completada al 100% según checklist de `tech_spec.md` para esa fase, o se reporta explícitamente qué quedó bloqueado y por qué.