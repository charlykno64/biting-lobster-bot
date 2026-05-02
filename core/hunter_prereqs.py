from __future__ import annotations

from typing import Any


def validate_hunter_search_objective(config: dict[str, Any]) -> tuple[bool, str]:
    """
    Comprueba que exista un objetivo de búsqueda mínimo para lanzar el hunter:
    al menos un equipo en target_teams y un límite de precio en centavos USD > 0.
    """
    criteria = config.get("search_criteria") or {}
    raw_teams = criteria.get("target_teams")
    if not isinstance(raw_teams, list):
        return False, "search_criteria.target_teams debe ser una lista con al menos un ID de equipo."
    teams = [str(t).strip() for t in raw_teams if str(t).strip()]
    if not teams:
        return False, "Debe definir al menos un equipo objetivo (país/equipo) en search_criteria.target_teams."

    raw_max = criteria.get("max_price_cents")
    try:
        max_cents = int(raw_max)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, "Debe definir un límite de precio válido (search_criteria.max_price_cents, entero > 0)."
    if max_cents <= 0:
        return False, "El límite de precio (max_price_cents) debe ser mayor que 0 (defina un tope en USD en el onboarding)."

    return True, ""
