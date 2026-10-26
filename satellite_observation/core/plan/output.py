"""
How a plan is written out: the list's texts, the CSV (`planCsv`) and the
scenes as a layer (`planFeatures`), as imaging-plan-panel.ts writes them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .access import Opportunity, PlanTarget
from .satellites import SatelliteSpec, satellite_label
from .scene import pass_shapes, scenes_taken

#: A TLE older than this many days is marked in the list.
OLD_TLE = 14


def js_round(v: float, digits: int = 1) -> str:
    """`String(Math.round(v * 10 ** digits) / 10 ** digits)` as JavaScript writes it."""
    scale = 10 ** digits
    x = math.floor(v * scale + 0.5) / scale
    if x == int(x):
        return str(int(x))
    return repr(x)


def js_number(v: float) -> str:
    """`String(v)` for a number, as JavaScript writes it (12, 12.5)."""
    return str(int(v)) if v == int(v) else repr(float(v))


def local_text(ms: float, seconds: bool = False) -> str:
    """A time in local time, `2026/10/11 10:23`."""
    d = datetime.fromtimestamp(ms / 1000)
    return d.strftime("%Y/%m/%d %H:%M:%S" if seconds else "%Y/%m/%d %H:%M")


def utc_text(ms: float) -> str:
    """`2026-10-11T01:23:45Z`."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def side_text(op: Opportunity) -> str:
    return f"{'右' if op.side == 'right' else '左'}・{'北行' if op.ascending else '南行'}"


def percent(v: float) -> str:
    return "100%" if v >= 0.995 else f"{max(1 if v > 0 else 0, math.floor(v * 100 + 0.5))}%"


def cover_text(op: Opportunity) -> str:
    """How much of the target a pass covers: a share for an area, scenes for a point or line."""
    if op.cells is not None:
        return "全域" if op.coverage >= 0.995 else percent(op.coverage)
    if op.width == 0 and op.frames <= 1:
        return "1 シーン"
    if op.strips * op.frames == 1:
        return f"1 シーン（幅 {js_round(op.width, 0)} km）"
    parts = (f"横 {op.strips}" if op.strips > 1 else "") + (" × " if op.strips > 1 and op.frames > 1 else "") + (f"パス方向 {op.frames}" if op.frames > 1 else "")
    return f"{op.strips * op.frames} シーン（{parts}）"


def cover_title(op: Opportunity, sat: SatelliteSpec) -> str:
    if op.cells is not None:
        return (f"この回は {scenes_taken(op, sat)} シーン（観測幅 {js_round(sat.swath)} km × シーン長 {js_round(sat.length)} km をパス方向に連続、"
                f"1 パス最大 {sat.scenesPerPass} シーン）で対象の {percent(op.coverage)} を撮像。対象全体は横 {op.strips} 列 × パス方向 {op.frames} 枚")
    if op.strips * op.frames > 1:
        return (f"幅 {js_round(op.width, 0)} km ÷ 観測幅 {js_round(sat.swath)} km で横に {op.strips} 列、"
                f"長さ {js_round(op.length, 0)} km ÷ シーン長 {js_round(sat.length)} km でパス方向に {op.frames} 枚")
    return ""


def mode_facts(sat: SatelliteSpec) -> str:
    """「・スポットライト（幅 10 km・15〜35°・分解能 1 m）」; empty for a satellite without modes."""
    if not sat.mode:
        return ""
    resolution = f"・分解能 {js_round(sat.resolution, 2)} m" if sat.resolution is not None else ""
    return f"・{sat.mode}（幅 {js_round(sat.swath)} km・{js_round(sat.minOffNadir)}〜{js_round(sat.maxOffNadir)}°{resolution}）"


def _cell(v: Any) -> str:
    text = "" if v is None else str(v)
    return '"' + text.replace('"', '""') + '"' if any(c in text for c in '",\r\n') else text


def plan_csv(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget], stations: Sequence = ()) -> str:
    """The CSV of a plan's opportunities (UTF-8 with a BOM and CRLF, for spreadsheets), as the viewer's."""
    combined = any(op.cumulative is not None for op in ops)
    prioritized = any(t.priority is not None for t in targets)
    clouded = any(op.cloud is not None for op in ops)
    downlinked = len(stations) > 0
    paired = any(op.pair for op in ops)
    moving = any(t.uncertainty is not None for t in targets)
    moded = any(s.mode for s in sats)
    head = [
        "日時（UTC）", "日時（ローカル）", "発注期限（ローカル）", "衛星",
        *(["撮像モード"] if moded else []),
        "衛星ID", "対象",
        *(["優先度"] if prioritized else []),
        "経度", "緯度",
        *(["位置の誤差km", "報告からの時間h"] if moving else []),
        "オフナディア角", "入射角", "方向", "軌道", "太陽高度", "幅km", "長さkm", "シーン数", "被覆率%",
        *(["雲量予報%"] if clouded else []),
        *(["受信見込み（ローカル）", "地上局"] if downlinked else []),
        *(["追加%", "累計%", "シーン番号", "同じシーンの地点数"] if combined else []),
        *(["2回目（UTC）", "2回目の衛星", "2回目のオフナディア角", "2回目の入射角", "交会角", "B/H", "垂直基線m", "間隔日"] if paired else []),
        "TLE経過日数",
    ]
    rows = []
    for op in ops:
        sat = sats[op.satellite]
        t = targets[op.target]
        row = [
            utc_text(op.time),
            local_text(op.time, True),
            local_text(op.time - sat.leadTime * 3600000, True) if sat.leadTime > 0 else "",
            sat.name,
            *([sat.mode or ""] if moded else []),
            sat.id,
            t.label,
            *([t.priority_text or ""] if prioritized else []),
            js_round(t.center[0], 6),
            js_round(t.center[1], 6),
            *([js_round(t.uncertainty or 0, 2), js_round((t.guessed or 0) / 3600000, 2)] if moving else []),
            js_round(op.offNadir, 2),
            js_round(op.incidence, 2),
            "右" if op.side == "right" else "左",
            "北行" if op.ascending else "南行",
            js_round(op.sunElevation, 1),
            js_round(op.width, 1),
            js_round(op.length, 1),
            op.strips * op.frames,
            js_round(op.coverage * 100, 1),
        ]
        if clouded:
            row.append(js_number(op.cloud) if op.cloud is not None else "")
        if downlinked:
            row += [local_text(op.downlink[0], True), stations[op.downlink[1]].name] if op.downlink else ["", ""]
        if combined:
            row += [js_round((op.gain or 0) * 100, 1), js_round((op.cumulative or 0) * 100, 1),
                    "" if op.shot is None else op.shot, "" if op.shotTargets is None else op.shotTargets]
        if paired:
            p = op.pair
            row += [utc_text(p.b.time), satellite_label(sats[p.b.satellite]), js_round(p.b.offNadir, 2), js_round(p.b.incidence, 2),
                    js_round(p.convergence, 2), js_round(p.bh, 3), math.floor(p.baseline + 0.5), js_round(p.days, 2)] if p else [""] * 8
        row.append(js_round(op.tleAge, 1))
        rows.append(row)
    return "﻿" + "\r\n".join(",".join(_cell(v) for v in r) for r in [head, *rows]) + "\r\n"


@dataclass
class LayerField:
    name: str
    #: `date` (ms, UTC), `string`, `double` or `integer`.
    kind: str
    alias: str


@dataclass
class PlanLayer:
    """Features for a layer: each a list of rings (lon, lat) and its attributes."""
    title: str
    fields: list
    features: list


def _r(v: float, digits: int) -> float:
    scale = 10 ** digits
    return math.floor(v * scale + 0.5) / scale


def plan_features(ops: Sequence[Opportunity], sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget], combined: bool,
                  stations: Sequence = ()) -> PlanLayer:
    """Opportunities' scenes as a layer: one feature per opportunity (a multipolygon), with its time and angles."""
    clouded = any(op.cloud is not None for op in ops)
    moded = any(s.mode for s in sats)
    paired = any(op.pair for op in ops)
    moving = any(t.uncertainty is not None for t in targets)
    features = []
    for op in ops:
        sat = sats[op.satellite]
        shapes = pass_shapes(sat, targets[op.target], op)
        if shapes is None:
            continue
        a: dict[str, Any] = {"time": op.time, "satellite": sat.name}
        if moded:
            a["mode"] = sat.mode
        a["satellite_id"] = sat.id
        a["target"] = targets[op.target].label
        if moving:
            a["uncertainty_km"] = _r(targets[op.target].uncertainty or 0, 2)
        a.update(off_nadir=_r(op.offNadir, 2), incidence=_r(op.incidence, 2), side="右" if op.side == "right" else "左",
                 **{"pass": "北行" if op.ascending else "南行"}, sun=_r(op.sunElevation, 1), scenes=len(shapes.scenes),
                 coverage=_r(op.coverage * 100, 1))
        if clouded:
            a["cloud"] = op.cloud
        if stations:
            a["downlink"] = op.downlink[0] if op.downlink else None
            a["station"] = stations[op.downlink[1]].name if op.downlink else None
        if paired:
            p = op.pair
            a.update(pair_time=p.b.time if p else None, pair_satellite=satellite_label(sats[p.b.satellite]) if p else None,
                     convergence=_r(p.convergence, 2) if p else None, bh=_r(p.bh, 3) if p else None,
                     baseline=math.floor(p.baseline + 0.5) if p else None, days=_r(p.days, 2) if p else None)
        if op.cumulative is not None:
            a.update(gain=_r((op.gain or 0) * 100, 1), cumulative=_r(op.cumulative * 100, 1), shot=op.shot, shot_targets=op.shotTargets)
        features.append((shapes.scenes, a))
    fields = [
        LayerField("time", "date", "撮像日時"),
        LayerField("satellite", "string", "衛星"),
        *([LayerField("mode", "string", "撮像モード")] if moded else []),
        LayerField("satellite_id", "string", "衛星ID"),
        LayerField("target", "string", "対象"),
        *([LayerField("uncertainty_km", "double", "位置の誤差km")] if moving else []),
        LayerField("off_nadir", "double", "オフナディア角"),
        LayerField("incidence", "double", "入射角"),
        LayerField("side", "string", "方向"),
        LayerField("pass", "string", "軌道"),
        LayerField("sun", "double", "太陽高度"),
        LayerField("scenes", "integer", "シーン数"),
        LayerField("coverage", "double", "被覆率%"),
        *([LayerField("cloud", "double", "雲量予報%")] if clouded else []),
        *([LayerField("downlink", "date", "受信見込み"), LayerField("station", "string", "地上局")] if stations else []),
        *([LayerField("pair_time", "date", "2回目"), LayerField("pair_satellite", "string", "2回目の衛星"), LayerField("convergence", "double", "交会角"),
           LayerField("bh", "double", "B/H"), LayerField("baseline", "double", "垂直基線m"), LayerField("days", "double", "間隔日")] if paired else []),
        *([LayerField("gain", "double", "追加%"), LayerField("cumulative", "double", "累計%"), LayerField("shot", "integer", "シーン番号"),
           LayerField("shot_targets", "integer", "同じシーンの地点数")] if combined else []),
    ]
    return PlanLayer("撮像計画", fields, features)


def split_antimeridian(coords: Sequence[tuple]) -> list:
    """A line cut where it crosses the antimeridian, so it does not sweep across the map."""
    parts: list = [[]]
    for i, c in enumerate(coords):
        if i > 0 and abs(c[0] - coords[i - 1][0]) > 180:
            parts.append([])
        parts[-1].append(c)
    return [p for p in parts if len(p) > 1]


def sort_value(key: str, op: Opportunity, sats: Sequence[SatelliteSpec], targets: Sequence[PlanTarget]):
    """The value the list sorts by for a column (as the viewer's `sorted`)."""
    if key == "satellite":
        return satellite_label(sats[op.satellite])
    if key == "target":
        return targets[op.target].label
    if key == "priority":
        p = targets[op.target].priority
        return math.inf if p is None else -p
    if key == "offNadir":
        return op.offNadir
    if key == "side":
        return side_text(op)
    if key == "sun":
        return op.sunElevation
    if key == "cloud":
        return 1000 if op.cloud is None else op.cloud
    if key == "downlink":
        return op.downlink[0] if op.downlink else math.inf
    if key == "cover":
        return op.coverage if op.cells is not None else op.strips * op.frames * 1e6 + op.width
    if key == "gain":
        return op.gain or 0
    if key == "cumulative":
        return op.target * 10 + (op.cumulative or 0)
    if key == "shot":
        return op.shot or 0
    if key == "uncertainty":
        return targets[op.target].uncertainty or 0
    return op.time


def natural_key(text: str) -> list:
    """Text ordered with its numbers as numbers (`SAT-2` before `SAT-10`), as the viewer's collator."""
    import re

    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold()) for part in re.split(r"(\d+)", text) if part]


def sorted_ops(ops: Sequence[Opportunity], key: str, descending: bool, sats, targets) -> list:
    """The opportunities sorted by a column, then by time (stable, as the viewer)."""
    import functools

    def compare(a: Opportunity, b: Opportunity) -> int:
        x, y = sort_value(key, a, sats, targets), sort_value(key, b, sats, targets)
        if isinstance(x, str) or isinstance(y, str):
            kx, ky = natural_key(str(x)), natural_key(str(y))
            order = (kx > ky) - (kx < ky)
        else:
            order = (x > y) - (x < y)
        order = -order if descending else order
        return order or (a.time > b.time) - (a.time < b.time)

    return sorted(ops, key=functools.cmp_to_key(compare))


def first_only(ops: Sequence[Opportunity]) -> list:
    """The first opportunity of each satellite and target."""
    seen: set = set()
    out = []
    for op in ops:
        k = (op.satellite, op.target)
        if k not in seen:
            seen.add(k)
            out.append(op)
    return out


def is_old_tle(op: Opportunity, sat: SatelliteSpec) -> bool:
    from .satellites import SAMPLE_SOURCE

    return sat.source != SAMPLE_SOURCE and abs(op.tleAge) > OLD_TLE


def describe_problems(problems: Sequence[str], too_soon: int) -> str:
    return (f"（計算できなかった衛星: {'、'.join(problems)}）" if problems else "") + \
           (f"（発注リードタイムに間に合わない機会 {too_soon} 回を除外）" if too_soon else "")


def summary_all(ops: Sequence[Opportunity], sats, problems_text: str) -> str:
    first = ops[0]
    return (f"{len(ops):,} 回。最短は {local_text(first.time)}（{satellite_label(sats[first.satellite])}、オフナディア角 {js_round(first.offNadir)}°）。"
            f"行を選ぶと地図に軌道と撮像範囲を表示{problems_text}")


def summary_cover(plans, targets, problems_text: str) -> str:
    def line(p) -> str:
        done = f"（{local_text(p.done)} 完了）" if p.done is not None else "（期間内に目標未達）"
        return f"{targets[p.target].label}: {len(p.steps)} パスで {percent(p.coverage)}{done}"

    done = [p for p in plans if p.done is not None]
    blocked = sum(p.blocked for p in plans)
    if len(plans) <= 3:
        summary = "、".join(line(p) for p in plans)
    else:
        last = f"（最後は {local_text(max(p.done for p in done))}）" if done else ""
        summary = f"{len(plans)} 対象のうち {len(done)} 対象が期間内に完了{last}"
    shots: dict = {}
    for step in (s for p in plans for s in p.steps):
        if step.shot is not None:
            shots[step.shot] = step.shotTargets or 1
    shared = [n for n in shots.values() if n > 1]
    scenes = ""
    if len(plans) > 1:
        scenes = f"全 {len(shots)} シーン" + (f"（うち {len(shared)} シーンで {sum(shared)} 地点をまとめて撮像）" if shared else "") + "。"
    ranked = "優先度の高い対象から撮像を割り当て。" if len({t.priority for t in targets}) > 1 else ""
    busy = f"撮像間隔・1 日の上限のため見送ったパス {blocked} 回。" if blocked else ""
    return f"{summary}。{scenes}{ranked}{busy}行を選ぶと、その回と同じ対象の他の回（番号つき）を地図に表示{problems_text}"


NO_OPPORTUNITY = "期間内に撮像できる機会なし。期間の延長、またはオフナディア角・入射角・軌道・向きの制限や日照条件の見直しが必要"
