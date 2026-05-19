"""Proxy Playwright/Chromium compartido (sticky session, mismas claves para CDP y hunter)."""

from __future__ import annotations

import random
import string
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

# Un solo sufijo sticky por proceso Python (Chrome CDP + Playwright hunter + handoff comparten IP).
_sticky_session_suffix: str | None = None


def canonical_proxy_endpoint_url(server: str) -> str:
    """
    URL del *endpoint* del proxy (no del sitio destino).

    - Sin esquema → ``http://host:puerto`` (Chrome/Playwright suelen exigirlo explícito).
    - ``https://host:puerto`` al proxy residencial/MITM → se reescribe a ``http://…`` (evita ERR_NO_SUPPORTED_PROXIES).
    - ``socks5://`` / ``socks4://`` se dejan igual.
    """
    s = (server or "").strip()
    if not s:
        return s
    low = s.lower()
    if low.startswith("socks5://") or low.startswith("socks4://"):
        return s
    if "://" not in s:
        return f"http://{s}"
    pu = urlparse(s)
    if pu.scheme in ("http", "https"):
        if pu.scheme == "https":
            return urlunparse(("http", pu.netloc, pu.path or "", "", pu.query, pu.fragment))
        return s
    return s


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
    return default


def resolve_playwright_proxy(hunter_cfg: dict[str, Any] | None) -> dict[str, str] | None:
    """
    Orden: hunter.playwright_proxy, luego hunter.camoufox_proxy (alias legado).

    Misma forma que espera Playwright: server, username, password.
    Sin usuario y contraseña no se usa proxy (evita --proxy-server sin credenciales y diálogo en Chrome CDP).

    sticky_session: si true (defecto), añade -session-<8 alfanum> al username Bright Data.
    """
    h = hunter_cfg or {}
    raw = h.get("playwright_proxy")
    if not isinstance(raw, dict):
        raw = h.get("camoufox_proxy")
    if not isinstance(raw, dict):
        return None
    server = canonical_proxy_endpoint_url(str(raw.get("server") or "").strip())
    if not server:
        return None
    username = str(raw.get("username") or "").strip()
    password = str(raw.get("password") or "").strip()
    if not username or not password:
        return None
    out: dict[str, str] = {"server": server}
    use_sticky = _coerce_bool(raw.get("sticky_session"), default=True)
    pw_lower = password.lower()
    # IPRoyal y similares: la sesión va en la contraseña (…_session-…_lifetime-…). No añadir -session- al usuario.
    session_in_password = "_session-" in pw_lower or (
        "session-" in pw_lower and ("lifetime" in pw_lower or "ttl" in pw_lower or "country-" in pw_lower)
    )
    if use_sticky and username and "-session-" not in username and not session_in_password:
        global _sticky_session_suffix
        if _sticky_session_suffix is None:
            _sticky_session_suffix = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        username = f"{username}-session-{_sticky_session_suffix}"
    if username:
        out["username"] = username
    if password:
        out["password"] = password
    return out


def playwright_ignore_https_errors_from_cfg(hunter_cfg: dict[str, Any] | None) -> bool:
    h = hunter_cfg or {}
    if _coerce_bool(h.get("playwright_ignore_https_errors"), default=False):
        return True
    return _coerce_bool(h.get("camoufox_ignore_https_errors"), default=False)


def chrome_proxy_cli_args(proxy: dict[str, str], *, embed_credentials: bool = True) -> list[str]:
    """
    --proxy-server=... para Chrome / args de Chromium.

    Esquema explícito ``http://`` al host del proxy (no ``https://`` salvo casos raros); sin esquema, Chrome
    puede responder ERR_NO_SUPPORTED_PROXIES. Con credenciales embebidas, el túnel al proxy sigue siendo HTTP.
    """
    server = canonical_proxy_endpoint_url((proxy.get("server") or "").strip())
    if not server:
        return []
    username = proxy.get("username") or ""
    password = proxy.get("password") or ""
    if embed_credentials and (username or password):
        parsed = urlparse(server)
        netloc = parsed.netloc
        if not netloc:
            return []
        # Conexión al proxy residencial = HTTP explícito (no https:// aunque venga en YAML).
        if parsed.scheme and parsed.scheme.startswith("socks"):
            scheme = parsed.scheme
        else:
            scheme = "http"
        uq = quote(str(username), safe="")
        pq = quote(str(password), safe="")
        proxy_url = f"{scheme}://{uq}:{pq}@{netloc}"
    else:
        proxy_url = server
    return [f"--proxy-server={proxy_url}"]


def chrome_extra_args_from_hunter_cfg(hunter_cfg: dict[str, Any] | None) -> list[str]:
    """
    Extra args al lanzar Chrome CDP desde la UI (proxy, etc.).

    hunter.chrome_cdp_ignore_certificate_errors: añade --ignore-certificate-errors (útil con proxy MITM).
    Junto va --test-type para atenuar el aviso de «marca de línea de comandos no admitida».
    """
    h = hunter_cfg or {}
    args: list[str] = []
    p = resolve_playwright_proxy(hunter_cfg)
    if p is not None:
        if "chrome_cdp_proxy_embed_credentials" in h:
            embed = _coerce_bool(h.get("chrome_cdp_proxy_embed_credentials"), default=False)
        else:
            # Por defecto: credenciales en URL (evita diálogo de proxy en Chrome CDP).
            embed = bool(p.get("username") and p.get("password"))
        args.extend(chrome_proxy_cli_args(p, embed_credentials=embed))
    if _coerce_bool(h.get("chrome_cdp_ignore_certificate_errors"), default=False):
        args.append("--ignore-certificate-errors")
        args.append("--test-type")
    return args


def chrome_cdp_manual_proxy_auth_hint(hunter_cfg: dict[str, Any] | None) -> str | None:
    """Solo si chrome_cdp_proxy_embed_credentials: false explícito en config (Chrome pedirá auth)."""
    h = hunter_cfg or {}
    if "chrome_cdp_proxy_embed_credentials" not in h:
        return None
    if _coerce_bool(h.get("chrome_cdp_proxy_embed_credentials"), default=False):
        return None
    p = resolve_playwright_proxy(hunter_cfg)
    if p is None:
        return None
    return (
        "Cuando Chrome CDP le solicite sus credenciales de Proxy copie y pegue las mismas del Onboarding."
    )
