"""
The satellite systems requests are handed to (`requestExports`): which
satellites each one has, its daily limits, and how its file is written
(CSV by keys, or JSON by a JSON Schema).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..time_zone import UTC, TimeZone, parse_time_zone
from ..util import is_number, is_record
from .link_id import ALPHABETS, DEFAULT_CHARS, DEFAULT_LENGTH, MAX_LENGTH, MIN_LENGTH
from .result_file import ResultFile, result_file_of

FORMATS = ("csv", "json")
#: How a request's own area is written in a system's file (`areaShape`, tsudo 2026-11-14): as it is (a polygon,
#: concave too), or the smallest rectangle turned any way that holds it (model.rotated_box).
AREA_SHAPES = {"polygon": "依頼の範囲のまま", "rotatedBox": "範囲を囲む回転した最小の矩形"}
ENCODINGS = {"utf-8": "utf-8", "utf8": "utf-8", "utf-8-sig": "utf-8-sig", "utf-8-bom": "utf-8-sig",
             "cp932": "cp932", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932"}


@dataclass
class Column:
    """One CSV column: its key (header), what goes in it, and how it is written."""
    key: str
    source: Optional[str] = None
    #: A fixed value (instead of a source).
    value: Any = None
    format: Optional[str] = None
    map: dict = field(default_factory=dict)
    geometry: Optional[str] = None
    digits: Optional[int] = None
    allowed: Optional[list] = None
    #: Written when the source's value is empty (none, "" or []); a list is joined by commas.
    default: Any = None


@dataclass
class PriorityRule:
    """How 1 … 1000 is written for a system: a table of bands (`max` → value), or a linear range; then a format."""
    table: list = field(default_factory=list)
    linear: Optional[dict] = None
    format: Optional[str] = None


@dataclass(frozen=True)
class UseTime:
    """
    衛星の使用時間 (`useTime`, tsudo 2026-11-14: 「システムによっては、軌道の
    使用時間で上限管理してる」「準備秒は足すだけでよい。定数はシステムのコン
    フィグに」「上限は1パスあたり」「1日の上限も欲しい」): seconds, each of the
    system's satellites on its own (all its modes together). core/request/use_time.py.
    """
    #: Added before the first shot of a pass (`setupSeconds`).
    setup: float = 0.0
    #: The most a satellite may be used on one pass (`perPass`); None: not limited.
    per_pass: Optional[float] = None
    #: The most a satellite may be used on one JST day (`perDay`); None: not limited.
    per_day: Optional[float] = None

    @property
    def limited(self) -> bool:
        return self.per_pass is not None or self.per_day is not None


def use_time_of(value: Any, at: str, problems: list[str]) -> UseTime:
    """`useTime`: {"setupSeconds": 秒, "perPass": 秒, "perDay": 秒} (each optional)."""
    if value is None:
        return UseTime()
    if not is_record(value):
        problems.append(f'{at} は {{"setupSeconds": 秒, "perPass": 秒, "perDay": 秒}} で指定')
        return UseTime()
    setup = value.get("setupSeconds", 0)
    if not (is_number(setup) and setup >= 0):
        problems.append(f"{at}.setupSeconds は 0 以上の数（秒）で指定")
        setup = 0
    limits = {}
    for key in ("perPass", "perDay"):
        n = value.get(key)
        if n is None:
            limits[key] = None
        elif is_number(n) and n > 0:
            limits[key] = float(n)
        else:
            problems.append(f"{at}.{key} は 0 より大きい数（秒）で指定（上限なしは書かない）")
            limits[key] = None
    return UseTime(float(setup), limits["perPass"], limits["perDay"])


@dataclass
class ExportSystem:
    """One entry of `requestExports`."""
    system: str
    label: str
    kind: str = "両方"
    series: list = field(default_factory=list)
    satellites: list = field(default_factory=list)
    order: float = 0
    #: Daily limits: {"count": n, "scenes": n}.
    limit: dict = field(default_factory=dict)
    #: Daily limits by category: {"定点": n, "臨時": n}.
    quota: dict = field(default_factory=dict)
    #: The scene (km, across and along) used to count a request's scenes when no mode says.
    scene_km: tuple = (10.0, 10.0)
    #: Whether requests go to the passes of its satellites' orbits (`orbit`; false: counted as before 2026-11-11).
    orbit: bool = True
    #: Whether requests close together on a pass are put into one shot, one row of its file (`merge`).
    merge: bool = True
    #: The room (metres) requests put into one shot leave inside it on every side (`mergeMargin`), so that the
    #: system, framing with its own orbit and scenes, still finds them inside one (tsudo, 2026-11-11: 「規定500m」).
    merge_margin: float = 500.0
    #: Whether an area too big for one shot that a satellite takes only part of is shared out to the system's other
    #: satellites (`share`; tsudo, 2026-11-12: 「効率を考えたら、相互に分担すべきでしょ？」).
    share: bool = True
    #: How a request's own area is written (`areaShape`, AREA_SHAPES): "polygon" as it is, "rotatedBox" the smallest
    #: rectangle turned any way holding it. Rows of requests put together or shared out are boxes already.
    area_shape: str = "polygon"
    format: str = "csv"
    folder: str = ""
    file_name: str = "{system}_{date:%Y%m%d}.{ext}"
    #: The most requests one file may hold (`maxPerFile`); more are split into numbered files. 0: no limit.
    max_per_file: int = 0
    encoding: str = "utf-8"
    header: bool = True
    time_zone: TimeZone = UTC
    date_format: str = "iso"
    priority: PriorityRule = field(default_factory=PriorityRule)
    #: Value maps by source (`{"lookSide": {"右": "R"}}`), for every column and property of that source.
    values: dict = field(default_factory=dict)
    columns: list = field(default_factory=list)
    #: The JSON Schema (an object) or where it is (a path, read when the file is written).
    schema: Any = None
    #: Places of the geometry (decimals) when nothing else says.
    digits: int = 6
    #: How the system's answer file (採否) is read; None when it sends none (answers by hand only).
    result_file: Optional[ResultFile] = None
    #: The 外部連携ID of its rows (core/request/link_id.py): how many characters (`linkIdLength`), of which (`linkIdChars`).
    link_length: int = DEFAULT_LENGTH
    link_chars: str = DEFAULT_CHARS
    #: 衛星の使用時間 (`useTime`): the setup seconds and the limits a pass and a day of each of its satellites.
    use_time: UseTime = field(default_factory=UseTime)


def _texts(v: Any) -> list:
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[,、]", v) if s.strip()]
    if isinstance(v, list):
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


def _column_of(v: Any, at: str, problems: list[str]) -> Optional[Column]:
    if not is_record(v) or not isinstance(v.get("key"), str) or not v["key"]:
        problems.append(f"{at} は key（列名）を持つオブジェクトで指定")
        return None
    source = v.get("source")
    if source is not None and not isinstance(source, str):
        problems.append(f"{at} の source は項目名で指定")
        source = None
    if source is None and "value" not in v:
        problems.append(f"{at} に source か value なし")
    digits = v.get("digits")
    allowed = v.get("allowed")
    return Column(
        key=v["key"], source=source, value=v.get("value"),
        format=v["format"] if isinstance(v.get("format"), str) else None,
        map=dict(v["map"]) if is_record(v.get("map")) else {},
        geometry=v["geometry"] if isinstance(v.get("geometry"), str) else None,
        digits=int(digits) if is_number(digits) and 0 <= digits <= 12 else None,
        allowed=list(allowed) if isinstance(allowed, list) else None,
        default=v.get("default"),
    )


def priority_rule_of(v: Any, at: str, problems: list[str]) -> PriorityRule:
    """`priority`: {table: [{max, value}…]} or {linear: {min, max, higherIsLarger}}, with `format`."""
    rule = PriorityRule()
    if v is None:
        return rule
    if not is_record(v):
        problems.append(f"{at} はオブジェクトで指定")
        return rule
    if isinstance(v.get("table"), list):
        rows = []
        for i, row in enumerate(v["table"]):
            if is_record(row) and is_number(row.get("max")) and "value" in row:
                rows.append((float(row["max"]), row["value"]))
            else:
                problems.append(f"{at}.table[{i}] は {{\"max\": 数, \"value\": 値}} で指定")
        rule.table = sorted(rows, key=lambda r: r[0])
    elif is_record(v.get("linear")):
        lin = v["linear"]
        if is_number(lin.get("min")) and is_number(lin.get("max")):
            rule.linear = {"min": float(lin["min"]), "max": float(lin["max"]), "higherIsLarger": bool(lin.get("higherIsLarger"))}
        else:
            problems.append(f"{at}.linear は min と max を数で指定")
    if isinstance(v.get("format"), str):
        rule.format = v["format"]
    return rule


def export_system_of(value: Any, problems: list[str], index: int) -> Optional[ExportSystem]:
    """The system of one `requestExports` entry; None (the reason in `problems`) when it cannot be used."""
    at = f"requestExports[{index}]"
    if not is_record(value):
        problems.append(f"{at} はオブジェクトで指定")
        return None
    name = value.get("system")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{at} に system（システム名）なし")
        return None
    fmt = str(value.get("format", "csv")).lower()
    if fmt not in FORMATS:
        problems.append(f"{at} の format は csv か json で指定")
        fmt = "csv"
    kind = value.get("kind", "両方")
    if kind not in ("光学", "SAR", "両方"):
        problems.append(f"{at} の kind は 光学・SAR・両方 のどれかで指定")
        kind = "両方"
    limit = {}
    if is_record(value.get("limit")):
        for k in ("count", "scenes"):
            n = value["limit"].get(k)
            if is_number(n) and n >= 0:
                limit[k] = int(n)
            elif k in value["limit"]:
                problems.append(f"{at} の limit.{k} は 0 以上の数で指定")
    quota = {}
    if is_record(value.get("quota")):
        for k, n in value["quota"].items():
            if k in ("定点", "臨時") and is_number(n) and n >= 0:
                quota[k] = int(n)
            else:
                problems.append(f"{at} の quota は {{\"定点\": 数, \"臨時\": 数}} で指定")
    scene = value.get("sceneKm")
    scene_km = (10.0, 10.0)
    if isinstance(scene, list) and len(scene) == 2 and all(is_number(x) and x > 0 for x in scene):
        scene_km = (float(scene[0]), float(scene[1]))
    elif is_number(scene) and scene > 0:
        scene_km = (float(scene), float(scene))
    elif scene is not None:
        problems.append(f"{at} の sceneKm は [幅, 長さ]（km）で指定")
    flags = {}
    for key in ("orbit", "merge", "share"):
        flags[key] = value.get(key, True)
        if not isinstance(flags[key], bool):
            problems.append(f"{at} の {key} は true か false で指定")
            flags[key] = True
    margin = value.get("mergeMargin", 500)
    if not (is_number(margin) and margin >= 0):
        problems.append(f"{at} の mergeMargin は 0 以上の数（メートル）で指定")
        margin = 500
    area_shape = value.get("areaShape", "polygon")
    if area_shape not in AREA_SHAPES:
        problems.append(f"{at} の areaShape は {'・'.join(AREA_SHAPES)} のどれかで指定")
        area_shape = "polygon"
    zone = UTC
    if "timeZone" in value:
        zone = parse_time_zone(value["timeZone"]) or UTC
        if parse_time_zone(value["timeZone"]) is None:
            problems.append(f"{at} の timeZone は Asia/Tokyo・UTC・+09:00 のように指定")
    per_file = value.get("maxPerFile")
    if per_file is not None and not (is_number(per_file) and per_file >= 1 and float(per_file).is_integer()):
        problems.append(f"{at} の maxPerFile は 1 以上の整数で指定")
        per_file = None
    link_length = value.get("linkIdLength", DEFAULT_LENGTH)
    if not (is_number(link_length) and MIN_LENGTH <= link_length <= MAX_LENGTH and float(link_length).is_integer()):
        problems.append(f"{at} の linkIdLength は {MIN_LENGTH} から {MAX_LENGTH} の整数（外部連携IDの文字数）で指定")
        link_length = DEFAULT_LENGTH
    link_chars = value.get("linkIdChars", DEFAULT_CHARS)
    if not isinstance(link_chars, str) or link_chars not in ALPHABETS:
        problems.append(f"{at} の linkIdChars は {'・'.join(ALPHABETS)} のどれかで指定")
        link_chars = DEFAULT_CHARS
    encoding = ENCODINGS.get(str(value.get("encoding", "utf-8")).lower())
    if encoding is None:
        problems.append(f"{at} の encoding は utf-8・utf-8-sig・cp932 のどれかで指定")
        encoding = "utf-8"
    columns = []
    if fmt == "csv":
        cols = value.get("columns")
        if isinstance(cols, list):
            columns = [c for c in (_column_of(c, f"{at}.columns[{i}]", problems) for i, c in enumerate(cols)) if c]
        if not columns:
            problems.append(f"{at} に columns（CSV の列）なし")
    schema = value.get("schema")
    if fmt == "json" and not (is_record(schema) or (isinstance(schema, str) and schema)):
        problems.append(f"{at} に schema（JSON Schema かそのファイル）なし")
    values = {k: dict(v) for k, v in value.get("values", {}).items() if is_record(v)} if is_record(value.get("values")) else {}
    label = value.get("label")
    digits = value.get("digits")
    return ExportSystem(
        system=name.strip(),
        label=label if isinstance(label, str) and label else name.strip(),
        kind=kind,
        series=_texts(value.get("series")),
        satellites=_texts(value.get("satellites")),
        order=float(value["order"]) if is_number(value.get("order")) else float(index),
        limit=limit,
        quota=quota,
        scene_km=scene_km,
        orbit=flags["orbit"],
        merge=flags["merge"],
        merge_margin=float(margin),
        share=flags["share"],
        area_shape=area_shape,
        format=fmt,
        folder=value["folder"] if isinstance(value.get("folder"), str) else "",
        file_name=value["fileName"] if isinstance(value.get("fileName"), str) and value["fileName"] else f"{{system}}_{{date:%Y%m%d}}.{fmt}",
        max_per_file=int(per_file) if per_file is not None else 0,
        encoding=encoding,
        header=value.get("header") is not False,
        time_zone=zone,
        date_format=value["dateFormat"] if isinstance(value.get("dateFormat"), str) and value["dateFormat"] else "iso",
        priority=priority_rule_of(value.get("priority"), f"{at}.priority", problems),
        values=values,
        columns=columns,
        schema=schema if is_record(schema) or isinstance(schema, str) else None,
        digits=int(digits) if is_number(digits) and 0 <= digits <= 12 else 6,
        result_file=result_file_of(value.get("resultFile"), f"{at}.resultFile", problems, zone, values),
        link_length=int(link_length),
        link_chars=link_chars,
        use_time=use_time_of(value.get("useTime"), f"{at}.useTime", problems),
    )


def export_systems_of(value: Any, problems: list[str]) -> list[ExportSystem]:
    """`requestExports`, in the order they are tried (`order`, then as written); a repeated system name is dropped."""
    if not isinstance(value, list):
        problems.append("requestExports は配列で指定")
        return []
    out: list[ExportSystem] = []
    seen: set = set()
    for i, v in enumerate(value):
        s = export_system_of(v, problems, i)
        if s is None:
            continue
        if s.system in seen:
            problems.append(f"requestExports の system「{s.system}」が重複")
            continue
        seen.add(s.system)
        out.append(s)
    return sorted(out, key=lambda s: s.order)
