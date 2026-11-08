"""
The observation report (報告書), as the viewer (observation-report.ts): the
records of a period (and of a class, and at least a level of 確度) made into
one page to print or keep — a summary, counts by class, activity and 確度, a
map of where things were, each thing's flow, the checked areas, the records
and the images. One HTML file, the map inside it as a data URL.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from .areas import CheckedArea, area_label
from .csv import local_time
from .flows import ObjectFlow, flows, living, time_key
from .geo import km_text, to_fixed
from .model import ConfidenceLevel, Observation
from .taxonomy import kind_of, path_of, within


@dataclass
class ReportOptions:
    """What goes into a report."""
    title: str = "観測報告"
    #: The period (ms, inclusive); None: open.
    start: Optional[int] = None
    end: Optional[int] = None
    #: Only this class and below ('' all).
    kind: str = ""
    #: Only records at least this sure (a level's label; '' all, records without a level too).
    confidence: str = ""
    map: bool = True
    flows: bool = True
    areas: bool = True
    records: bool = False


@dataclass
class ReportImage:
    id: str
    name: str
    time: Optional[int]


@dataclass
class ReportSelection:
    """The records and areas a report holds."""
    records: list[Observation]
    flows: list[ObjectFlow]
    areas: list[CheckedArea]
    #: The images they are from, in time order.
    images: list[ReportImage] = field(default_factory=list)


def _in_period(t: Optional[int], o: ReportOptions) -> bool:
    return (o.start is None or (t is not None and t >= o.start)) and (o.end is None or (t is not None and t <= o.end))


def select_for_report(everything: Iterable[Observation], areas: Iterable[CheckedArea], options: ReportOptions,
                      levels: Sequence[ConfidenceLevel]) -> ReportSelection:
    """The records (living), things and areas of the period, class and 確度 asked for."""
    kind = path_of(options.kind)
    labels = [lv.label for lv in levels]

    def rank(label: Optional[str]) -> int:
        return labels.index(label) if label in labels else -1

    limit = rank(options.confidence) if options.confidence else -1
    records = [o for o in living(everything)
               if _in_period(time_key(o), options) and (not kind or within(o.kind, kind))
               and (limit < 0 or 0 <= rank(o.confidence) <= limit)]
    shown = [a for a in areas
             if a.deleted_at is None and _in_period(a.image_time if a.image_time is not None else a.created_at, options)
             and (not kind or not a.kind or within(a.kind, kind) or within(kind_of(kind), path_of(a.kind)))]
    images: dict[str, ReportImage] = {}
    for x in [*records, *shown]:
        if x.image_id not in images:
            images[x.image_id] = ReportImage(x.image_id, x.image_name or x.image_id, x.image_time)
    return ReportSelection(
        records=records,
        flows=flows(records),
        areas=shown,
        images=sorted(images.values(), key=lambda i: (i.time if i.time is not None else float("inf"), i.name)),
    )


def _escape(v) -> str:
    return html.escape("" if v is None else str(v), quote=True).replace("&#x27;", "'")


def _table(head: Sequence[str], rows: Iterable[Sequence]) -> str:
    return ("<table><thead><tr>" + "".join(f"<th>{_escape(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")


def _counts(values: Iterable[str]) -> list[tuple[str, int]]:
    """Counts of a value, the most first."""
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass
class ReportContext:
    """What the page says besides the selection."""
    #: The store the records are from.
    store: str
    #: Who made the report.
    author: str
    made: int
    levels: Sequence[ConfidenceLevel] = ()
    #: The map (a data URL), when drawn.
    map: Optional[str] = None


STYLE = """body{font:13px/1.55 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;color:#111;margin:24px auto;max-width:960px;padding:0 16px}
h1{font-size:22px;margin:0 0 8px}h2{font-size:16px;border-bottom:2px solid #111;padding-bottom:2px;margin:22px 0 8px}h3{font-size:14px;margin:12px 0 2px}h3 small{font-weight:400;color:#555}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:0}dl.meta dt{color:#555}dl.meta dd{margin:0}
table{border-collapse:collapse;width:100%;margin:4px 0 8px}th,td{border:1px solid #bbb;padding:2px 6px;text-align:left;vertical-align:top}th{background:#f1f1f1}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.summary{font-size:14px}.note{color:#555;margin:0}
.map img{width:100%;border:1px solid #bbb}.thing{break-inside:avoid}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}}"""


def report_html(selection: ReportSelection, options: ReportOptions, context: ReportContext) -> str:
    """The report as one HTML page (styles inside, the map as a data URL)."""
    records, areas, images = selection.records, selection.areas, selection.images
    things = selection.flows
    times = [time_key(o) for o in records]
    if options.start is not None or options.end is not None:
        period = f"{local_time(options.start) if options.start is not None else ''} 〜 {local_time(options.end) if options.end is not None else ''}"
    elif times:
        period = f"{local_time(min(times))} 〜 {local_time(max(times))}（記録の期間）"
    else:
        period = "—"
    meta = [
        ("期間", period),
        ("分類", f"{kind_of(path_of(options.kind))} 以下" if options.kind else "すべて"),
        ("確度", f"{options.confidence} 以上" if options.confidence else "すべて"),
        ("保存先", context.store),
        ("作成", f"{local_time(context.made)}{'・' + context.author if context.author else ''}"),
    ]
    parts: list[str] = [f"<h1>{_escape(options.title or '観測報告')}</h1>",
                        '<dl class="meta">' + "".join(f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>" for k, v in meta) + "</dl>"]
    present = [f for f in things if f.present]
    parts.append(f'<section><h2>概要</h2><p class="summary">モノ {len(things)} 件（最新で所在 {len(present)}・不在 {len(things) - len(present)}）・'
                 f'記録 {len(records)} 件・画像 {len(images)} 枚{f"・確認範囲 {len(areas)} 件" if options.areas else ""}</p>')
    by_class = _counts((path_of(f.kind) or ["種別なし"])[0] for f in things)
    by_activity = _counts(f.activity or "活動の記録なし" for f in present)
    by_confidence = _counts(o.confidence or "確度なし" for o in records)
    parts.append(f'<div class="cols">{_table(["大分類", "モノ"], by_class)}{_table(["最新の活動", "モノ"], by_activity)}{_table(["確度", "記録"], by_confidence)}</div></section>')
    if options.map and context.map:
        parts.append(f'<section class="map"><h2>位置</h2><img src="{_escape(context.map)}" alt="記録の位置" /></section>')
    if options.flows and things:
        parts.append("<section><h2>モノごとの流れ</h2>")
        for f in things:
            last = f.observations[-1]
            latest = (f.activity or "活動の記録なし") if f.present else "不在"
            line = f"{local_time(f.first)} 〜 {local_time(f.last)}・{f.seen} 画像で確認・移動 {km_text(f.distance)}・最新: {latest}{f'（{last.confidence}）' if last.confidence else ''}"
            steps = [[local_time(s.start) if s.start == s.end else f"{local_time(s.start)} 〜 {local_time(s.end)}",
                      (s.activity or "—") if s.present else "不在", s.count, km_text(s.moved) if s.moved is not None and s.moved > 0 else ""]
                     for s in f.steps]
            parts.append(f'<article class="thing"><h3>{_escape(f.name)} <small>{_escape(f.kind or "種別なし")}・{_escape(f.object_id)}</small></h3>'
                         f"<p>{_escape(line)}</p>" + _table(["期間", "活動", "画像", "移動"], steps) + "</article>")
        parts.append("</section>")
    if options.areas and areas:
        rows = [[local_time(a.image_time), a.image_name or a.image_id, re.sub(r"^確認範囲（(.*)）$", r"\1", area_label(a)), a.note, a.recorded_by or ""]
                for a in sorted(areas, key=lambda a: a.image_time if a.image_time is not None else a.created_at)]
        parts.append('<section><h2>確認範囲</h2><p class="note">範囲内の対象の種別は記録がすべて（記録のないものは不在）</p>'
                     + _table(["撮像日時", "画像", "対象", "備考", "記録者"], rows) + "</section>")
    if options.records and records:
        rows = [[local_time(o.image_time), o.object_name, o.kind, o.activity if o.present else "不在", o.confidence or "",
                 to_fixed(o.lon, 5), to_fixed(o.lat, 5), o.note, o.recorded_by or ""]
                for o in sorted(records, key=lambda o: (time_key(o), o.object_name))]
        parts.append("<section><h2>記録一覧</h2>" + _table(["撮像日時", "モノ", "種別", "活動", "確度", "経度", "緯度", "備考", "記録者"], rows) + "</section>")
    parts.append("<section><h2>画像</h2>" + _table(["撮像日時", "画像", "画像 ID"], [[local_time(i.time) or "日時不明", i.name, i.id] for i in images]) + "</section>")
    title = _escape(options.title or "観測報告")
    body = "\n".join(parts)
    return (f'<!doctype html>\n<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>\n'
            f"<style>\n{STYLE}\n</style></head><body>\n{body}\n</body></html>\n")


_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def report_file_name(title: str, made: int) -> str:
    """The report's file name: 観測報告_20261025.html"""
    stamp = datetime.fromtimestamp(made / 1000).strftime("%Y%m%d")
    return _UNSAFE.sub("_", title or "観測報告") + f"_{stamp}.html"
