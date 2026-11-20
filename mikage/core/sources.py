"""
The settings file's Esri layers and tables (URL, or { url, fields, where, token }) and their attributes by
role, as the features read them (通報's rules, 衛星の障害情報, 関心地域, 引き継ぎ, 担当…).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .util import is_name, is_record, layer_url

_FEATURE_LAYER = re.compile(r"/(Feature|Map)Server/\d+/?$", re.IGNORECASE)
_EDITABLE = re.compile(r"/FeatureServer/\d+/?$", re.IGNORECASE)


@dataclass
class EsriSource:
    """An Esri layer or table and its attribute names by role."""
    url: str
    fields: dict[str, str]
    token: Optional[str] = None
    where: Optional[str] = None


def esri_source_of(v: Any, at: str, defaults: dict[str, str], problems: list[str], editable: bool = False,
                   ) -> Optional[EsriSource]:
    """
    An Esri layer or table of the settings file (URL, or { url, fields, where, token }), its attributes by
    role (`defaults`, renamed by `fields`); None (the reason in `problems`) when it cannot be used.
    """
    v = {"url": v} if isinstance(v, str) else v
    pattern = _EDITABLE if editable else _FEATURE_LAYER
    if not is_record(v) or not isinstance(v.get("url"), str) or not pattern.search(v["url"].split("?", 1)[0]):
        what = "編集できる …/FeatureServer/0" if editable else "…/FeatureServer/0"
        problems.append(f"{at} は {what} のようなレイヤー・テーブルの URL で指定")
        return None
    fields = dict(defaults)
    if is_record(v.get("fields")):
        for k, name in v["fields"].items():
            if k not in defaults:
                problems.append(f"{at}.fields.{k} は使用不可の役割（{'・'.join(defaults)}）")
            elif is_name(name):
                fields[k] = name
            else:
                problems.append(f"{at}.fields.{k} は属性名で指定")
    token, where = v.get("token"), v.get("where")
    return EsriSource(
        url=layer_url(v["url"]), fields=fields,
        token=token if isinstance(token, str) and token else None,
        where=where.strip() if isinstance(where, str) and where.strip() else None,
    )
