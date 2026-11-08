"""
Reading what the day's request files need from outside the settings file: a
system's JSON Schema given as a path (beside the settings file, or a URL;
read in a task, as it may be fetched).
"""
from __future__ import annotations

import os
import re
from urllib.parse import urljoin

from ..core.config import load_jsonc
from . import settings
from .network import get_bytes


def is_url(value) -> bool:
    """An http(s) URL (not a path)."""
    return isinstance(value, str) and bool(re.match(r"^https?://", value, re.IGNORECASE))


def schema_base() -> str:
    """Where relative schema paths start: the settings file's folder (or URL), else the plugin's folder."""
    source, _ = settings.effective_config_source()
    if source and is_url(source):
        return source
    if source and os.path.isfile(source):
        return os.path.dirname(os.path.abspath(source))
    return settings.PLUGIN_DIR


def load_schema(path: str) -> dict:
    """A JSON Schema file (JSON or JSONC); raises OSError / ValueError when it cannot be read."""
    base = schema_base()
    if is_url(path) or is_url(base) and not os.path.isabs(path):
        url = path if is_url(path) else urljoin(base, path)
        text = get_bytes(url, settings.auth_for(url)).decode("utf-8-sig")
    else:
        full = path if os.path.isabs(path) else os.path.join(base, path)
        with open(full, encoding="utf-8-sig") as f:
            text = f.read()
    value = load_jsonc(text)
    if not isinstance(value, dict):
        raise ValueError("JSON Schema はオブジェクトで指定")
    return value
