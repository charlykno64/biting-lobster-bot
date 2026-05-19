"""
Mapeo FIFA: ID de equipo/país (`<option value>` en #team) → nombre visible en la tienda.

Estructura preparada para varios idiomas; el hunter usa español por defecto (`hunter.lang` / `es`).
Ampliar TEAM_MAPPING_* o añadir claves según el HTML del listado FIFA.
"""

from __future__ import annotations

from typing import Final

# Español — alineado con tech_spec.md / <select id="team"> (tienda FIFA es-MX)
TEAM_MAPPING_ES: Final[dict[str, str]] = {
    "11404606582": "Alemania",
    "11404606664": "Arabia Saudí",
    "11404606510": "Argelia",
    "11404606516": "Argentina",
    "11404606519": "Australia",
    "11404606520": "Austria",
    "11404606533": "Bosnia y Herzegovina",
    "11404606535": "Brasil",
    "11404606527": "Bélgica",
    "11404606541": "Cabo Verde",
    "10229225507167": "Canadá",
    "11404606656": "Catar",
    "11404606550": "Colombia",
    "11404606504": "Costa de Marfil",
    "11404606556": "Croacia",
    "11404606558": "Curasao",
    "11404606565": "Ecuador",
    "10229225507169": "EEUU",
    "11404606566": "Egipto",
    "11404606665": "Escocia",
    "11404606677": "España",
    "11404606577": "Francia",
    "11404606583": "Ghana",
    "11404606592": "Haití",
    "11404606568": "Inglaterra",
    "11404606600": "Irak",
    "11404606604": "Japón",
    "11404606605": "Jordania",
    "11404606634": "Marruecos",
    "10229225507168": "México",
    "11404606646": "Noruega",
    "11404606641": "Nueva Zelanda",
    "11404606650": "Panamá",
    "11404606652": "Paraguay",
    "11404606639": "Países Bajos",
    "11404606654": "Portugal",
    "11404606553": "RD del Congo",
    "11404606560": "República Checa",
    "11404606609": "República de Corea",
    "11404606599": "RI de Irán",
    "11404606666": "Senegal",
    "11404606675": "Sudáfrica",
    "11404606684": "Suecia",
    "11404606685": "Suiza",
    "11404606696": "Turquía",
    "11404606695": "Túnez",
    "11404606702": "Uruguay",
    "11404606704": "Uzbekistán",
}

# Futuro: TEAM_MAPPING_EN, etc.
TEAM_MAPPINGS_BY_LANG: Final[dict[str, dict[str, str]]] = {
    "es": TEAM_MAPPING_ES,
}


def resolve_team_country_name(team_id: str, *, lang: str = "es") -> str | None:
    """Nombre para teclear en Select2; None si el ID no está en el diccionario del idioma."""
    tid = str(team_id).strip()
    if not tid:
        return None
    lang_key = (lang or "es").strip().lower()[:2]
    mapping = TEAM_MAPPINGS_BY_LANG.get(lang_key) or TEAM_MAPPING_ES
    return mapping.get(tid)
