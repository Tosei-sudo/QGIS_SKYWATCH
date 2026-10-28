"""
`imageRules` of the viewer's `config.json` (config.ts `imageRuleOf`,
image-rules.ts): by an image's file name, the first rule whose pattern
matches gives the correction, the bands shown and the values that are no
data when the image opens.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union
from urllib.parse import unquote, urlsplit

from .pipeline import PRESETS, Pipeline, enhance_of


@dataclass
class ImageRule:
    label: str
    match: "re.Pattern"
    pipeline: Optional[Pipeline] = None
    #: R, G, B (or one band in gray): 1-based numbers or band names.
    bands: Optional[list] = None
    #: {"values": [...]} and "auto": False to leave the file's own no-data value out; None for nothing.
    no_data: Optional[dict] = None


def image_rule_of(value: Any, problems: list, index: int) -> Optional[ImageRule]:
    at = f"imageRules[{index}]"
    if not isinstance(value, dict):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    match = value.get("match")
    if not isinstance(match, str) or not match:
        problems.append(f"{at} に match（ファイル名の正規表現）なし")
        return None
    try:
        # File names on Windows and most servers ignore case, so the patterns do too unless told.
        pattern = re.compile(match, 0 if value.get("caseSensitive") is True else re.IGNORECASE)
    except re.error as error:
        problems.append(f"{at} の match は正規表現として読み込み不可（{error}）")
        return None

    pipeline: Optional[Pipeline] = None
    preset = value.get("preset")
    if preset is not None:
        if isinstance(preset, str) and preset in PRESETS:
            pipeline = PRESETS[preset]
        else:
            problems.append(f"{at} の preset「{preset}」は {'・'.join(PRESETS)} のいずれかで指定")
    if "enhance" in value:
        pipeline = enhance_of(value["enhance"], pipeline, problems, at)

    bands = None
    if "bands" in value:
        raw = value["bands"]
        listed = raw if isinstance(raw, list) else [raw]

        def valid(b) -> bool:
            if isinstance(b, bool):
                return False
            if isinstance(b, int) or (isinstance(b, float) and b.is_integer()):
                return b >= 1
            return isinstance(b, str) and b.strip() != ""

        if len(listed) in (1, 3) and all(valid(b) for b in listed):
            bands = [int(b) if isinstance(b, (int, float)) else b for b in listed]
        else:
            problems.append(f"{at} の bands は [赤, 緑, 青] か 1 つのバンド（1 から数えた番号かバンド名）で指定")

    no_data = None
    if "noData" in value:
        raw = value["noData"]

        def numbers(v) -> list:
            return [float(n) for n in (v if isinstance(v, list) else [v])
                    if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)]

        if isinstance(raw, (int, float, list)) and not isinstance(raw, bool):
            no_data = {"values": numbers(raw)}
        elif isinstance(raw, dict) and (raw.get("auto") is None or isinstance(raw.get("auto"), bool)):
            no_data = {**({"auto": False} if raw.get("auto") is False else {}), "values": numbers(raw.get("values", []))}
        else:
            problems.append(f'{at} の noData は値（数値か数値の配列）か {{ "auto": false, "values": [0] }} の形で指定')

    if pipeline is None and bands is None and no_data is None:
        problems.append(f"{at} は補正（preset・enhance）もバンド（bands）もノーデータ（noData）も未指定のため無視")
        return None
    label = value.get("label")
    return ImageRule(label if isinstance(label, str) and label else match, pattern, pipeline, bands, no_data)


def image_rules_of(value: Any, problems: list) -> list:
    if not isinstance(value, list):
        problems.append("imageRules は配列で指定")
        return []
    return [r for r in (image_rule_of(v, problems, i) for i, v in enumerate(value)) if r is not None]


def file_name_of(name: str) -> str:
    """The file name of a name as opened: the last part of a URL's path (decoded, without the query), or of a path."""
    if re.match(r"^([a-z][a-z\d+.-]+:|\.{0,2}/)", name, re.IGNORECASE) and not re.match(r"^[a-z]:[\\/]", name, re.IGNORECASE):
        path = urlsplit(name).path if re.match(r"^[a-z][a-z\d+.-]+:", name, re.IGNORECASE) else name.split("?")[0].split("#")[0]
        return unquote(path[path.rfind("/") + 1:])
    return name[max(name.rfind("/"), name.rfind("\\")) + 1:]


def image_rule_for(rules: Sequence[ImageRule], name: str) -> Optional[ImageRule]:
    """The first rule whose pattern finds a match in the file name of `name`."""
    file = file_name_of(name)
    return next((r for r in rules if r.match.search(file)), None)


def resolve_bands(bands: Sequence[Union[int, str]], count: int, names: Sequence[str]) -> tuple:
    """1-based band numbers for a rule's bands (numbers or names, case and spaces ignored). Raises ValueError with the reason."""
    if len(bands) == 3 and count < 3:
        raise ValueError(f"バンドが {count} つのみ")
    out = []
    for b in bands:
        if isinstance(b, int):
            if b > count:
                raise ValueError(f"バンド {b} なし（{count} バンドの画像）")
            out.append(b)
            continue
        wanted = b.strip().lower()
        found = next((i + 1 for i, n in enumerate(names) if (n or "").strip().lower() == wanted), None)
        if found is None:
            raise ValueError(f"「{b}」という名前のバンドなし")
        out.append(found)
    return tuple(out)
