"""
The place search of the settings file's `geocoders` (core/geocode.py), as
「座標・地名へ移動」 asks it (ui/coordinate_box.py) and as 依頼文から下書き looks
up a place name (ui/request_draft.py): a relative geocoder URL against the
settings file's place, a file read from disk, an Esri layer asked with its
token.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from ..core.errors import FileFailed, os_code
from ..core.geocode import PlaceSearch
from . import settings
from .network import get_bytes


def resolve_url(context, url: str) -> str:
    """A geocoder's URL as asked: a relative one against the settings file's place."""
    source = context.source or ""
    if re.match(r"^https?://", url, re.IGNORECASE):
        return url
    if url.startswith("//"):
        scheme = urlparse(source).scheme if re.match(r"^https?://", source, re.IGNORECASE) else "https"
        return f"{scheme}:{url}"
    if re.match(r"^https?://", source, re.IGNORECASE):
        return urljoin(source, url)
    base = os.path.dirname(source) if source else settings.PLUGIN_DIR
    return os.path.normpath(url if os.path.isabs(url) else os.path.join(base, url))


def read_bytes(url: str) -> bytes:
    """A geocoder's answer, or its file (a CSV of names for a closed network)."""
    if re.match(r"^https?://", url, re.IGNORECASE):
        return get_bytes(url, settings.auth_for(url))
    path = unquote(urlparse(url).path) if url.startswith("file:") else url
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as error:
        raise FileFailed(f"地名の一覧「{os.path.basename(path)}」を読めない", code=os_code(error), detail=f"{path}: {error.strerror or error}") from error


def place_search_of(context) -> PlaceSearch:
    """The place search of the settings now (none asked when the settings have no `geocoders`)."""

    def ask_esri(url: str, params: dict, token: Optional[str]) -> dict:
        return context.esri().ask(url, params, context.token_for(url, token))

    return PlaceSearch(context.config.geocoders, read_bytes, ask_esri, lambda url: resolve_url(context, url))
