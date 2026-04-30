from __future__ import annotations

import json
import os
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError, URLError


class LicenseRepository:
    def __init__(self, table_name: str = "licenses") -> None:
        self.table_name = table_name
        raw_base_url = os.getenv("SUPABASE_API_URL") or os.getenv("SUPABASE_URL") or ""
        self.base_url = self._normalize_base_url(raw_base_url)
        self.api_key = os.getenv("SUPABASE_KEY") or ""

    def _normalize_base_url(self, base_url: str) -> str:
        cleaned = base_url.strip().rstrip("/")
        if not cleaned:
            return ""
        if cleaned.endswith("/rest/v1"):
            return cleaned
        return f"{cleaned}/rest/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _ready(self) -> bool:
        return bool(self.base_url and self.api_key)

    def upsert_license(self, hardware_id: str) -> dict[str, Any]:
        if not self._ready():
            return {"access_granted": "LIMITED", "tickets_secured": 0, "source": "local_fallback"}

        existing = self.get_license(hardware_id)
        if existing:
            return existing

        # Match database schema in `database/supabase_schema.sql`.
        # `licenses` does not define `tickets_secured`, so posting it causes HTTP 400.
        payload = json.dumps({"hardware_id": hardware_id, "access_granted": "LIMITED"}).encode("utf-8")
        endpoint = f"{self.base_url.rstrip('/')}/{self.table_name}"
        req = request.Request(endpoint, data=payload, headers=self._headers(), method="POST")
        try:
            with request.urlopen(req, timeout=8) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return {"access_granted": "LIMITED", "tickets_secured": 0, "source": "local_fallback"}

        return body[0] if isinstance(body, list) and body else {"access_granted": "LIMITED", "tickets_secured": 0}

    def get_license(self, hardware_id: str) -> dict[str, Any] | None:
        if not self._ready():
            return None
        query = parse.urlencode({"hardware_id": f"eq.{hardware_id}", "select": "*", "limit": "1"})
        endpoint = f"{self.base_url.rstrip('/')}/{self.table_name}?{query}"
        req = request.Request(endpoint, headers=self._headers(), method="GET")
        try:
            with request.urlopen(req, timeout=8) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return None
        if isinstance(body, list) and body:
            return body[0]
        return None
