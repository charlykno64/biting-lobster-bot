from __future__ import annotations

import json
from urllib import request
from urllib.error import URLError


ALLOWED_COUNTRIES = {"mexico", "united states", "canada"}


def check_geolocation_allowed(timeout_seconds: int = 8) -> tuple[bool, str]:
    try:
        req = request.Request("http://ip-api.com/json/")
        with request.urlopen(req, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError):
        return False, "No fue posible validar geolocalizacion con ip-api.com."

    country = str(payload.get("country", "")).strip()
    if country.lower() in ALLOWED_COUNTRIES:
        return True, country
    return False, country or "Desconocido"
