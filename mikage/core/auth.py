"""
ArcGIS portals (`arcgisPortals`) of the settings file, as the viewer reads
them (auth.ts). In QGIS the sign-in itself is an authentication
configuration (OAuth2 / ESRI token) chosen per store in the plugin settings;
the portals are only offered as hints for setting those up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from .util import is_record

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@dataclass
class ArcgisPortalConfig:
    label: str
    #: The portal's root (without /sharing/rest).
    url: str
    client_id: str


def _secure(url: str):
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    if not u.scheme or not u.netloc or u.username or u.password:
        return None
    if u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in _LOCAL_HOSTS):
        return u
    return None


def arcgis_portal_of(value: Any, problems: list[str], index: int) -> Optional[ArcgisPortalConfig]:
    """The portal of one `arcgisPortals` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"arcgisPortals[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    url, client_id, label = value.get("url"), value.get("clientId"), value.get("label")
    u = _secure(url.strip()) if isinstance(url, str) else None
    if not u or u.query or u.fragment:
        problems.append(f"{at} の url はポータルの https の URL（https://www.arcgis.com、https://gis.example.com/portal など）で指定")
        return None
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id):
        problems.append(f"{at} の clientId（ポータルに登録したアプリのクライアント ID）なし")
        return None
    path = u.path or "/"
    root = f"{u.scheme}://{u.netloc.lower()}{path}".rstrip("/")
    root = re.sub(r"/sharing(/rest)?$", "", root, flags=re.IGNORECASE)
    return ArcgisPortalConfig(label.strip() if isinstance(label, str) and label.strip() else u.netloc.lower(), root, client_id)
