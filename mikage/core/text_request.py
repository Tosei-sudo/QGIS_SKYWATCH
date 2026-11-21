"""
依頼文から下書き (requestDraft; tsudo 2026-11-21: 「撮像計画系にミニ生成AIの力を
使える部分はあるかな」 → 「1番良さそうなので、始めて」, design doc 「依頼文から撮像
依頼を下書き 設計方針案」, and 「４だけ編集でも出し、「12月末まで延長」のような文で
名指した項目を直すように。他は提案のまま」): an imaging request written in a mail
or a chat made into the request form's fields, for the person to look over
and take item by item. Nothing is saved by it.

What the words themselves name is read here, and it wins over the model:

- the days: 「来週月曜から2週間」「12月末まで」「11/25〜30」「明日」「12月上旬」
  (days ahead: a date without a year is this year's, or next year's when it
  would be long past);
- numbers with their units: 「雲量30%以下」 (→ 実施しない雲量 31%),
  「分解能50cm」, 「オフナディア角20度以下」, 「1日2回」;
- how often: 「毎週月・木」 (定点・曜日), 「隔日」「3日ごと」 (定点・間隔),
  「毎日」, 「期間中に1回」 (臨時・期間内1回);
- the satellites, series and modes of the catalog (their aliases too);
- words of a closed set: 光学・SAR, 昇交・降交, 右・左観測, 昼・夜, 至急・
  余裕があれば, 管理番号: …, and when editing 停止・再開.

The model (about 3B) gives the rest in lines 「項目: 値」: a short name, the
place names, who asks; and its reading of what the code found too, which is
kept only where the words give it a footing (a number it writes must be in
the text, a name must be in the text, a choice must be a choice).

The area is never made by the model: the places are looked up in what is
there (ui/request_draft.py: the coordinates written, the named areas, the
requests before, the geocoders).

Editing a request, only what the text names is proposed, and only where it
differs from the request: 「12月末まで延長」 changes the period's end and
nothing else; the name, the area and the requester only when the text says
to change them (「名称を…に」「範囲を…に変更」).

Pure Python; the screens are ui/request_draft.py.
"""
from __future__ import annotations

import calendar
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple, Optional, Sequence

from .request.api import PRIORITY_LEVELS, ImagingRequest, day_bounds, jst_day, repeat_of, square_rings
from .text_assist import Message, TextTask, numbers_in
from .text_tasks import answer_lines, bare

#: The lines the model is asked for.
REQUEST_KEYS = ("名称", "地名", "期間", "頻度", "優先度", "雲量", "分解能", "オフナディア角", "入射角", "時間帯", "1日の回数", "依頼者")
#: The most of a text sent to the model (characters; the code reads all of it).
TEXT_LIMIT = 1500
#: The longest name proposed.
NAME_LIMIT = 40
#: A date written without a year this many days or more before today is next year's.
PAST_DAYS = 60
WEEKDAYS = "月火水木金土日"

#: The items of a draft in the form's order: (key, label).
ITEMS = (
    ("name", "名称"), ("management_no", "管理番号"), ("identification_no", "識別番号"), ("status", "状態"),
    ("category", "区分"), ("achieve", "達成条件"), ("period", "期間"), ("repeat", "繰り返し"), ("times", "1 日の回数"),
    ("priority", "優先度"), ("kind", "種別"), ("look_side", "観測方向"), ("orbit_dir", "軌道方向"), ("time_of_day", "時間帯"),
    ("satellites", "衛星"), ("off_nadir", "オフナディア角"), ("incidence", "入射角"), ("resolution", "分解能"),
    ("cloud_no_go", "実施しない雲量"), ("requester", "依頼者"),
)
LABELS = dict(ITEMS)


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def _nfkc(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    return re.sub("[～〜~]", "〜", t)


# ---- the days -------------------------------------------------------------------------------------------


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _ahead(year: Optional[str], month: int, day: int, today: date) -> Optional[date]:
    """A day written: without a year this year's, or next year's when it would be long past."""
    try:
        d = date(int(year) if year else today.year, month, day)
    except ValueError:
        return None
    if not year and d < today - timedelta(days=PAST_DAYS):
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d


def future_hints(today: date) -> list[tuple[str, date, date]]:
    """The periods a request names most, as dates (今日, 明日, 来週 = 月〜日 …); the model only copies them."""
    monday = today - timedelta(days=today.weekday())
    next_month = _add_months(today.replace(day=1), 1)
    return [
        ("今日", today, today), ("明日", today + timedelta(days=1), today + timedelta(days=1)),
        ("明後日", today + timedelta(days=2), today + timedelta(days=2)),
        ("今週", today, monday + timedelta(days=6)), ("来週", monday + timedelta(days=7), monday + timedelta(days=13)),
        ("再来週", monday + timedelta(days=14), monday + timedelta(days=20)),
        ("今月", today, _month_end(today)), ("来月", next_month, _month_end(next_month)),
        ("年内", today, date(today.year, 12, 31)),
    ]


_UNIT_DAYS = {"日": 1, "日間": 1, "週": 7, "週間": 7}
_MONTH_WORDS = ("か月", "ヶ月", "カ月", "ケ月", "ヵ月", "箇月", "ヶ月間", "か月間", "カ月間")
_KANJI = "〇一二三四五六七八九十"


def _count(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    from .text_assist import _kanji_value

    return _kanji_value(s)


@dataclass
class _Day:
    """A date (or days) written, where it is, and what it is to the period."""
    at: int
    end_at: int
    first: date
    last: date
    #: "start" (…から), "end" (…まで), "both" (a range), "" (not said).
    role: str = ""
    words: str = ""


# A day: 2026-11-25, 2026年11月25日, 11/25, 11月25日 (a weekday in brackets after it is left).
_YMD = r"(?:(?P<y>\d{4})\s*[-/.年]\s*)?(?P<m>\d{1,2})\s*[/月]\s*(?P<d>\d{1,2})\s*日?(?:\s*[(（][月火水木金土日](?:曜日?)?[)）])?"
_YMD2 = r"(?:(?:(?P<y2>\d{4})\s*[-/.年]\s*)?(?P<m2>\d{1,2})\s*[/月]\s*)?(?P<d2>\d{1,2})\s*日?(?:\s*[(（][月火水木金土日](?:曜日?)?[)）])?"
_RANGE = re.compile(_YMD + r"\s*(?:〜|から|より|-|–|—|ー)\s*" + _YMD2 + r"(?!\s*[/月\d])")
_ONE_DAY = re.compile(_YMD)
#: 12月, 2026年12月, 12月上旬・中旬・下旬・末・中・いっぱい.
_MONTH = re.compile(r"(?:(?P<y>\d{4})\s*年\s*)?(?<![\d/])(?P<m>\d{1,2})\s*月(?![\d/])\s*(?P<part>上旬|中旬|下旬|末日?|中|いっぱい|一杯)?")
_RELATIVE = re.compile(r"(?P<w>再来週|来週|今週|今月|来月|年内|今年中|年末|月末|本日|今日|明日|あす|明後日|あさって)"
                       r"(?P<part>中|末|いっぱい|一杯)?")
_WEEKDAY = re.compile(r"(?P<pre>再来週|来週|今週|次の|次回の|今度の|翌週)\s*の?\s*(?P<w>[月火水木金土日])\s*曜日?")
_START = re.compile(r"^\s*(?:の)?\s*(?:から|より|以降|以後|〜|開始)")
_END = re.compile(r"^\s*(?:まで|迄|以前|までに|が期限|を期限|締め?切)")
_END_BEFORE = re.compile(r"(?:期限|締め?切り?|〆切?)\s*(?:は|:|：)?\s*$")
_DURATION = re.compile(r"^\s*(?:から|より)?\s*(?:の)?\s*(?P<n>\d+|[〇一二三四五六七八九十]+)\s*(?P<u>日間|日|週間|週|か月間?|ヶ月間?|カ月間?|ケ月間?|ヵ月間?)")
#: Words that a text names a period with (the model's reading is taken only then).
DATE_CUE = re.compile(r"\d|[月日週年]|まで|から|今日|明日|本日|あす|あさって")


def _days_in(t: str, today: date) -> list[_Day]:
    """The dates written in `t` (NFKC), in order, each with its role."""
    found: list[_Day] = []
    taken = [False] * len(t)

    def free(a: int, b: int) -> bool:
        return not any(taken[a:b])

    def take(a: int, b: int) -> None:
        for i in range(a, b):
            taken[i] = True

    for m in _RANGE.finditer(t):
        first = _ahead(m["y"], int(m["m"]), int(m["d"]), today)
        if first is None or not free(m.start(), m.end()):
            continue
        month = int(m["m2"]) if m["m2"] else first.month
        year = m["y2"] or (m["y"] if m["y"] and not m["m2"] else None)
        try:
            last = date(int(year), month, int(m["d2"])) if year else date(first.year, month, int(m["d2"]))
        except ValueError:
            continue
        if last < first and not year:
            try:
                last = last.replace(year=last.year + 1)
            except ValueError:
                continue
        if last < first:
            continue
        found.append(_Day(m.start(), m.end(), first, last, "both", m[0]))
        take(m.start(), m.end())
    for m in _ONE_DAY.finditer(t):
        if not free(m.start(), m.end()):
            continue
        d = _ahead(m["y"], int(m["m"]), int(m["d"]), today)
        if d is not None:
            found.append(_Day(m.start(), m.end(), d, d, "", m[0]))
            take(m.start(), m.end())
    for m in _MONTH.finditer(t):
        if not free(m.start(), m.end()):
            continue
        first = _ahead(m["y"], int(m["m"]), 1, today)
        if first is None:
            continue
        if not m["y"] and _month_end(first) < today:
            first = first.replace(year=first.year + 1)
        last = _month_end(first)
        part = m["part"] or ""
        if part == "上旬":
            last = first.replace(day=10)
        elif part == "中旬":
            first, last = first.replace(day=11), first.replace(day=20)
        elif part == "下旬":
            first = first.replace(day=21)
        elif part.startswith("末"):
            first = last
        found.append(_Day(m.start(), m.end(), max(first, today) if first.month == today.month and first.year == today.year
                          and not part.startswith("末") else first, last, "", m[0]))
        take(m.start(), m.end())
    for m in _WEEKDAY.finditer(t):
        if not free(m.start(), m.end()):
            continue
        monday = today - timedelta(days=today.weekday())
        w = WEEKDAYS.index(m["w"])
        pre = m["pre"]
        if pre in ("次の", "次回の", "今度の"):
            d = today + timedelta(days=(w - today.weekday()) % 7 or 7)
        else:
            weeks = {"今週": 0, "来週": 1, "翌週": 1, "再来週": 2}[pre]
            d = monday + timedelta(days=7 * weeks + w)
        found.append(_Day(m.start(), m.end(), d, d, "", m[0]))
        take(m.start(), m.end())
    for m in _RELATIVE.finditer(t):
        if not free(m.start(), m.end()):
            continue
        word, part = m["w"], m["part"] or ""
        hints = {name: (a, b) for name, a, b in future_hints(today)}
        name = {"本日": "今日", "あす": "明日", "あさって": "明後日", "今年中": "年内", "年末": "年内", "月末": "今月"}.get(word, word)
        first, last = hints[name]
        if word in ("年末", "月末") or part.startswith("末"):
            if name in ("今週", "来週", "再来週"):
                # 週末: Saturday and Sunday.
                first = max(today, last - timedelta(days=1))
            else:
                first = last
        found.append(_Day(m.start(), m.end(), first, last, "", m[0]))
        take(m.start(), m.end())
    found.sort(key=lambda d: d.at)
    for d in found:
        after, before = t[d.end_at:d.end_at + 8], t[max(0, d.at - 8):d.at]
        if d.role != "both":
            if _START.match(after):
                d.role = "start"
            elif _END.match(after) or _END_BEFORE.search(before):
                d.role = "end"
        if d.role == "start":
            m = _DURATION.match(after)
            n = _count(m["n"]) if m else None
            if m and n and 0 < n <= 3660:
                unit = m["u"]
                if unit.startswith(("か", "ヶ", "カ", "ケ", "ヵ")):
                    last = _add_months(d.first, n) - timedelta(days=1)
                else:
                    last = d.first + timedelta(days=n * _UNIT_DAYS.get(unit, 1) - 1)
                d.last, d.role = last, "both"
                d.words += after[:m.end()].rstrip()
    return found


class Period(NamedTuple):
    """The days a text names: None for a side it does not name."""
    start: Optional[date] = None
    end: Optional[date] = None
    words: str = ""


def period_of(text: str, today: date) -> Period:
    """
    The period a request names: 「来週月曜から2週間」 → that Monday to 13 days
    later, 「12月末まで」 → only the end, 「11/25〜30」, 「明日」 → that day.
    """
    t = _nfkc(text)
    days = _days_in(t, today)
    if not days:
        return Period()
    start = next((d.first for d in days if d.role in ("start", "both")), None)
    end = next((d.last for d in reversed(days) if d.role in ("end", "both")), None)
    loose = [d for d in days if not d.role]
    if start is None and end is None and loose:
        start, end = min(d.first for d in loose), max(d.last for d in loose)
    elif loose and start is not None and end is None and len(days) == 1:
        end = start
    words = "・".join(d.words.strip() for d in days)
    if start and end and end < start:
        return Period(start, None, words)
    return Period(start, end, words)


_EXTEND = re.compile(r"(?P<n>\d+|[〇一二三四五六七八九十]+)\s*(?P<u>日|週間|週|か月|ヶ月|カ月|ケ月|ヵ月)\s*(?:間)?\s*(?:の)?\s*(?:延長|延ば|伸ば)")


def extension_of(text: str, end: Optional[date]) -> Optional[date]:
    """「1週間延長」 of a request ending on `end`: its new end."""
    m = _EXTEND.search(_nfkc(text))
    n = _count(m["n"]) if m else None
    if not m or not n or end is None:
        return None
    if m["u"].startswith(("か", "ヶ", "カ", "ケ", "ヵ")):
        return _add_months(end, n)
    return end + timedelta(days=n * _UNIT_DAYS.get(m["u"], 1))


# ---- how often ------------------------------------------------------------------------------------------

_WEEKLY = re.compile(r"(?:毎週|週ごとに|週毎に)\s*の?\s*((?:[月火水木金土日](?:曜日?)?\s*(?:[・、,，/／と及]|および|かつ)?\s*)+)")
_WEEKDAYS_LIST = re.compile(r"((?:[月火水木金土日]曜日?\s*(?:[・、,，/／と]|および)\s*)+[月火水木金土日]曜日?)\s*(?:に|の)?\s*(?:毎週|ごと|毎|撮)")
_EVERY = re.compile(r"(?P<n>\d+|[一二三四五六七八九十]+)\s*日\s*(?P<w>ごと|毎|置き|おき|に1回|に一回|に1度|に一度)")
_DAILY = re.compile(r"毎日|連日|日々|毎朝|毎夕|毎晩")
_ONCE = re.compile(r"(?:期間中|期間内|期限まで|いずれか|どこか|どれか)[^。\n]{0,10}(?:1|一)\s*(?:回|度)"
                   r"|(?<![日]に)(?<!1日)(?<!一日)(?:1|一)\s*(?:回|度)\s*(?:だけ|のみ|で(?:よい|良い|いい|十分|結構)|撮れれば|撮れたら)"
                   r"|一度(?:だけ|で)")
_STEADY = re.compile(r"定点|定期|継続|監視を続|ずっと")
_ONE_OFF = re.compile(r"臨時|スポット|単発|今回のみ")


@dataclass(frozen=True)
class Often:
    """How often: kind (毎日 / 曜日 / 間隔 / 1回), the weekdays (0 = Monday), the days apart, and the words."""
    kind: str
    weekdays: tuple = ()
    every: int = 0
    words: str = ""

    @property
    def text(self) -> str:
        if self.kind == "曜日":
            return "曜日:" + "".join(WEEKDAYS[d] for d in self.weekdays)
        if self.kind == "間隔":
            return f"間隔:{self.every}"
        return "毎日"


def often_of(text: str) -> Optional[Often]:
    """How often a text asks for images (None when it does not say)."""
    t = _nfkc(text)
    m = _WEEKLY.search(t) or _WEEKDAYS_LIST.search(t)
    if m:
        days = tuple(sorted({WEEKDAYS.index(c) for c in re.sub(r"曜日?", "", m[1]) if c in WEEKDAYS}))
        if days:
            return Often("曜日", days, words=m[0].strip())
    if re.search(r"隔日|一日置き|1日置き|一日おき|1日おき", t):
        return Often("間隔", every=2, words=re.search(r"隔日|一日置き|1日置き|一日おき|1日おき", t)[0])
    m = _EVERY.search(t)
    n = _count(m["n"]) if m else None
    if m and n:
        every = n + 1 if m["w"] in ("置き", "おき") else n
        if 2 <= every <= 365:
            return Often("間隔", every=every, words=m[0])
        if every == 1:
            return Often("毎日", words=m[0])
    if re.search(r"毎週(?!\s*[月火水木金土日])", t):
        return Often("間隔", every=7, words="毎週")
    m = _DAILY.search(t)
    if m:
        return Often("毎日", words=m[0])
    m = _ONCE.search(t)
    if m:
        return Often("1回", words=m[0])
    return None


def _often_of_model(value: str, text: str) -> Optional[Often]:
    """The model's 頻度, kept when the text gives it a footing."""
    v = _nfkc(bare(value))
    t = _nfkc(text)
    if re.search(r"1\s*回|一回|一度|1度", v) and re.search(r"(?:1|一)\s*(?:回|度)|までに|いずれか|どこか", t):
        return Often("1回", words=bare(value))
    if "毎日" in v and _DAILY.search(t):
        return Often("毎日", words=bare(value))
    if "曜" in v:
        days = tuple(sorted({WEEKDAYS.index(c) for c in v if c in WEEKDAYS}))
        if days and all(f"{WEEKDAYS[d]}曜" in t or re.search(rf"毎週[^。]*{WEEKDAYS[d]}", t) for d in days):
            return Often("曜日", days, words=bare(value))
    m = re.search(r"(\d+)\s*日\s*(?:ごと|毎)", v)
    if m and 2 <= int(m[1]) <= 365 and m[1] in numbers_in(t):
        return Often("間隔", every=int(m[1]), words=bare(value))
    return None


# ---- the other items -----------------------------------------------------------------------------------

#: Words that say how soon, for each priority level (a level's own name after 優先度 counts too).
PRIORITY_CUES: dict[str, tuple[str, ...]] = {
    "最優先": ("最優先", "大至急", "至急", "緊急", "最重要", "超特急", "大急ぎ", "asap"),
    "高": ("優先度高", "優先度は高", "優先度:高", "高優先", "優先して", "優先で", "急ぎ", "早めに", "なるべく早く", "できるだけ早く"),
    "中": ("優先度中", "優先度は中", "優先度:中", "通常", "普通の優先"),
    "低": ("優先度低", "優先度は低", "優先度:低", "低優先", "余裕があれば", "手が空いたら", "急ぎません", "急ぎではな",
          "急ぎではあり", "急ぎじゃな", "急ぎでな", "急がな", "急ぎでは無", "いつでも"),
    "最低": ("優先度最低", "優先度は最低", "優先度:最低", "空いていれば", "空きがあれば", "ついでに", "ついでで"),
}
_KIND_CUES = {"SAR": ("sar", "レーダ", "合成開口", "radar"), "光学": ("光学", "可視", "optical", "パンクロ", "マルチスペクトル", "カラー画像")}
_ASCENDING = ("昇交", "アセンディング", "ascending", "北行")
_DESCENDING = ("降交", "ディセンディング", "descending", "南行")
_LOOK = re.compile(r"(右|左)\s*(?:側)?\s*(?:観測|ルック|視|向き|look)|(right|left)[- ]?look", re.IGNORECASE)
_NIGHT = re.compile(r"夜間|夜(?!明)|ナイト|night", re.IGNORECASE)
_DAY = re.compile(r"昼間|日中|昼(?!夜)|デイタイム|daytime", re.IGNORECASE)
_BOTH_TIMES = re.compile(r"昼夜|昼でも夜でも|昼も夜も|時間帯は問わ")
#: 「夜間もOK」: either time; 「夜は不要」: the other time.
_ANY_TIME = re.compile(r"(?:昼間|日中|昼|夜間|夜)\s*(?:でも|も)\s*(?:OK|ok|可|いい|よい|良い|構わ|構い|かまわ|かまい|大丈夫|問題な|問わ)")
_NOT_TIME = re.compile(r"(昼間|日中|昼|夜間|夜)\s*(?:は|の撮像は|の撮影は)?\s*(?:不要|いらな|要らな|なし|無し|不可|除く|除外|NG|ng|ダメ|だめ)")
_STOP = re.compile(r"停止|中止|取りやめ|取り止め|キャンセル|不要になっ|やめ(?:て|る|ます)")
_RESTART = re.compile(r"再開")


def _earliest(t: str, cues: dict[str, tuple[str, ...]]) -> Optional[tuple[str, str]]:
    """The level whose cue comes first in `t` (folded), the longer cue first at one place: (level, cue)."""
    best: Optional[tuple[int, int, str, str]] = None
    for level, words in cues.items():
        for w in words:
            at = t.find(fold(w))
            if at >= 0 and (best is None or (at, -len(w)) < (best[0], best[1])):
                best = (at, -len(w), level, w)
    return (best[2], best[3]) if best else None


def priority_of(text: str) -> Optional[tuple[int, str]]:
    """The priority a text names: (the level's value, the words), None when it names none."""
    t = re.sub(r"[\s　]", "", fold(text)).replace("：", ":")
    got = _earliest(t, PRIORITY_CUES)
    if got is None:
        return None
    level, words = got
    value = next(v for name, *_rest, v in PRIORITY_LEVELS if name == level)
    return value, words


def kind_of(text: str) -> Optional[str]:
    t = fold(text)
    said = [k for k, cues in _KIND_CUES.items() if any(c in t for c in cues)]
    if len(said) == 1:
        return said[0]
    return "任意" if len(said) == 2 else None


def orbit_of(text: str) -> Optional[str]:
    t = fold(text)
    up, down = any(c in t for c in _ASCENDING), any(c in t for c in _DESCENDING)
    return "昇交" if up and not down else "降交" if down and not up else None


def look_of(text: str) -> Optional[str]:
    sides = {("右" if (m[1] == "右" or (m[2] or "").lower() == "right") else "左") for m in _LOOK.finditer(_nfkc(text))}
    return sides.pop() if len(sides) == 1 else None


def time_of_day_of(text: str) -> Optional[str]:
    """昼 or 夜 when the text asks for one (「夜は不要」 asks for 昼); None for either or not said."""
    t = _nfkc(text)
    if _BOTH_TIMES.search(t) or _ANY_TIME.search(t):
        return None
    no = {"夜" if m[1] in ("夜", "夜間") else "昼" for m in _NOT_TIME.finditer(t)}
    rest = _NOT_TIME.sub("", t)
    night, day = bool(_NIGHT.search(rest)) or no == {"昼"}, bool(_DAY.search(rest)) or no == {"夜"}
    if no == {"夜"}:
        night = False
    elif no == {"昼"}:
        day = False
    return "夜" if night and not day else "昼" if day and not night else None


_NUM = r"(\d+(?:\.\d+)?)"
_CLOUD = re.compile(r"雲(?:量|の量|被覆率?|の割合)?\s*(?:は|が|:)?\s*" + _NUM + r"\s*(%|パーセント|割)\s*"
                    r"(以下|まで|以内|未満|より少な|を超え|超|以上|より多)?[^。\n]{0,12}?(中止|不要|不可|見送|実施しない|撮らない|やめ|NG)?",
                    re.IGNORECASE)


def cloud_of(text: str) -> Optional[tuple[float, str]]:
    """
    The 実施しない雲量 a text names (this cloud or more: no image), and the words:
    「雲量30%以下」 → 31, 「30%未満」 → 30, 「30%以上なら中止」 → 30, 「3割まで」 → 31.
    """
    m = _CLOUD.search(_nfkc(text))
    if not m:
        return None
    v = float(m[1]) * (10 if m[2] == "割" else 1)
    how = m[3] or ""
    if how in ("未満", "より少な", "以上"):
        no_go = v
    else:
        # 以下・まで・以内・を超え・超・より多 (and none): more than v is no image.
        no_go = v + 1
    if not 0 <= no_go <= 101:
        return None
    return min(no_go, 100.0), m[0].strip()


_RESOLUTION = re.compile(r"(?:地上)?(?:分解能|解像度|gsd)\s*(?:は|が|:)?\s*" + _NUM + r"\s*(cm|センチ|m(?![a-z])|メートル)"
                         r"|" + _NUM + r"\s*(cm|センチ|m(?![a-z])|メートル)\s*(?:級|以下の分解能|分解能|解像度)", re.IGNORECASE)


def resolution_of(text: str) -> Optional[tuple[float, str]]:
    """The 分解能 (metres) a text names: 「分解能1m以下」 → 1, 「50cm級」 → 0.5."""
    m = _RESOLUTION.search(_nfkc(text))
    if not m:
        return None
    n, unit = (m[1], m[2]) if m[1] else (m[3], m[4])
    v = float(n) / (100 if unit.lower() in ("cm", "センチ") else 1)
    return (v, m[0].strip()) if 0.01 <= v <= 1000 else None


def _angle_re(names: str) -> tuple[re.Pattern, re.Pattern]:
    head = r"(?:" + names + r")\s*(?:角)?\s*(?:は|が|:)?\s*"
    deg = r"\s*(?:度|°)?"
    span = re.compile(head + _NUM + deg + r"\s*(?:〜|から|-)\s*" + _NUM + deg, re.IGNORECASE)
    one = re.compile(head + _NUM + deg + r"\s*(以下|まで|以内|未満|以上|より大き|超)?", re.IGNORECASE)
    return span, one


OFF_NADIR = _angle_re(r"オフナディア|off[- ]?nadir|ona")
INCIDENCE = _angle_re(r"入射|incidence")


def angles_of(text: str, which: tuple[re.Pattern, re.Pattern]) -> Optional[tuple[Optional[float], Optional[float], str]]:
    """(lowest, highest, words) of an angle a text names (None for a side not named)."""
    t = _nfkc(text)
    m = which[0].search(t)
    if m:
        low, high = sorted((float(m[1]), float(m[2])))
        return (low, high, m[0].strip()) if high < 90 else None
    m = which[1].search(t)
    if not m:
        return None
    v = float(m[1])
    if not 0 <= v < 90:
        return None
    if m[2] in ("以上", "より大き", "超"):
        return v, None, m[0].strip()
    return None, v, m[0].strip()


_TIMES_SPAN = re.compile(r"(?:1日|一日|日)\s*(?:に|あたり)?\s*(\d+)\s*(?:〜|から|-)\s*(\d+)\s*回")
_TIMES = re.compile(r"(?:1日|一日|日)\s*(?:に|あたり)?\s*(\d+|[一二三四五六七八九十]+)\s*回")


def times_of(text: str) -> Optional[tuple[int, Optional[int], str]]:
    """(at least, at most, words) of 1 日の回数 (「1日2回」「1日2〜3回」); None for once or not said."""
    t = _nfkc(text)
    m = _TIMES_SPAN.search(t)
    if m:
        low, high = sorted((int(m[1]), int(m[2])))
        return (low, high, m[0]) if 1 <= low and high <= 24 else None
    m = _TIMES.search(t)
    n = _count(m[1]) if m else None
    if m and n and 2 <= n <= 24:
        return n, None, m[0]
    return None


def number_of(label: str, text: str) -> Optional[tuple[str, str]]:
    """「管理番号: A-123」: (the value, the words)."""
    m = re.search(label + r"\s*(?:は|:)?\s*([A-Za-z0-9][A-Za-z0-9\-_/.#]*)", _nfkc(text))
    return (m[1].rstrip("."), m[0]) if m else None


_REQUESTER = re.compile(r"(?:依頼者|依頼元|担当者?|連絡先)\s*(?:は|:)?\s*([^\s、。,(（\n]{1,20}(?:[ 　][^\s、。,(（\n]{1,10})?)")
#: Edit: the text says to change these (not only names them to say which request).
_CHANGE_NAME = re.compile(r"(?:名称|件名|名前|タイトル)\s*(?:を|は|:)")
_CHANGE_AREA = re.compile(r"(?:範囲|場所|地点|地名|対象地|エリア|撮像範囲)\s*(?:を|は|:)|に(?:変更|変えて|差し替え)")
_CHANGE_REQUESTER = re.compile(r"(?:依頼者|依頼元|担当)\s*(?:を|は|:)")


# ---- satellites -----------------------------------------------------------------------------------------


def _name_pattern(name: str) -> Optional[re.Pattern]:
    """A satellite's name as written in a text: 「ALOS-2」 also 「ALOS2」「alos 2」; letters not run on."""
    parts = [p for p in re.split(r"[\s\-_・./]+", unicodedata.normalize("NFKC", name).strip()) if p]
    if not parts:
        return None
    body = r"[\s\-_・./]?".join(re.escape(p) for p in parts)
    plain = "".join(parts)
    if len(plain) < 2 or (plain.isascii() and len(plain) < 3 and not any(c.isdigit() for c in plain)):
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Satellites:
    """The satellites a text names, as the request keeps them (core/request/pick.py)."""
    series: tuple = ()
    satellites: tuple = ()
    modes: tuple = ()
    words: str = ""

    @property
    def empty(self) -> bool:
        return not (self.series or self.satellites or self.modes)

    @property
    def text(self) -> str:
        parts = []
        if self.series:
            parts.append("シリーズ " + "・".join(self.series))
        if self.satellites:
            parts.append("・".join(self.satellites))
        if self.modes:
            parts.append("モード " + "・".join(self.modes))
        return "、".join(parts)


def satellites_of(text: str, sats: Sequence[Any]) -> Satellites:
    """
    The series, satellites and modes of the catalog (`sats`: SatelliteSpec)
    that `text` names (by name, id or alias): a satellite named is kept by
    name (in the order written); a series only when none of its satellites
    is named; a mode when named.
    """
    t = _nfkc(text)
    hits: list[tuple[int, str, str]] = []  # (where, which list, name)
    seen: set = set()

    def look(kind: str, written: str, *names: Optional[str]) -> None:
        key = (kind, written)
        if key in seen:
            return
        for n in names:
            p = _name_pattern(n) if n else None
            m = p.search(t) if p else None
            if m:
                seen.add(key)
                hits.append((m.start(), kind, written))
                return

    for s in sats:
        look("satellites", s.name, s.name, s.id, *tuple(getattr(s, "aliases", ()) or ()))
    for s in sats:
        if s.series:
            look("series", s.series, s.series)
    for s in sats:
        mode = getattr(s, "mode", None)
        if mode:
            look("modes", mode, mode)
    hits.sort()
    named = [n for _, k, n in hits if k == "satellites"]
    named_series = {s.series for s in sats if s.name in named}
    series = [n for _, k, n in hits if k == "series" and n not in named_series and fold(n) not in {fold(x) for x in named}]
    modes = [n for _, k, n in hits if k == "modes" and fold(n) not in {fold(x) for x in named + series}]
    words = "・".join(dict.fromkeys(t[a:a + 24].split()[0] if t[a:a + 24].split() else "" for a, _k, _n in hits))
    return Satellites(tuple(series), tuple(dict.fromkeys(named)), tuple(dict.fromkeys(modes)), words)


# ---- places ---------------------------------------------------------------------------------------------

_COORD_PATTERNS = (
    re.compile(r"[北南]緯\s*[\d.]+\s*(?:度\s*(?:[\d.]+\s*分\s*(?:[\d.]+\s*秒)?)?)?\s*[、,\s]*[東西]経\s*[\d.]+\s*(?:度\s*(?:[\d.]+\s*分\s*(?:[\d.]+\s*秒)?)?)?"),
    re.compile(r"[NS]?\s*-?\d{1,2}\.\d{2,}\s*°?\s*[NS]?\s*[,、\s/]\s*[EW]?\s*-?\d{1,3}\.\d{2,}\s*°?\s*[EW]?"),
    re.compile(r"(?<![A-Za-z0-9])\d{1,2}[C-HJ-NP-X]\s?[A-HJ-NP-Z]{2}\s?\d{2,5}\s?\d{0,5}(?![A-Za-z0-9])"),
)


def points_of(text: str) -> list[tuple[float, float]]:
    """The coordinates written in a text (lon, lat), in order."""
    from .coords import parse_coordinate

    t = unicodedata.normalize("NFKC", text)
    out: list[tuple[int, tuple[float, float]]] = []
    taken: list[tuple[int, int]] = []
    for pattern in _COORD_PATTERNS:
        for m in pattern.finditer(t):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            p = parse_coordinate(m[0])
            if p is not None:
                out.append((m.start(), (p[0], p[1])))
                taken.append((m.start(), m.end()))
    return [p for _, p in sorted(out)]


def _place_word(word: str) -> bool:
    """Whether a word can be a place's name: 2 characters or more, or one kanji (呉・津)."""
    w = fold(word)
    return len(w) >= 2 or (len(w) == 1 and "\u4e00" <= w <= "\u9fff")


def places_in(text: str, names: Sequence[str]) -> list[str]:
    """The names of `names` (named areas…) written in `text`, the longer first where one holds another, in order."""
    t = fold(text)
    found: list[tuple[int, str]] = []
    for n in sorted({n for n in names if _place_word(n)}, key=lambda n: -len(n)):
        at = t.find(fold(n))
        if at >= 0 and not any(a <= at < a + len(fold(o)) for a, o in found):
            found.append((at, n))
    return [n for _, n in sorted(found)]


@dataclass(frozen=True)
class AreaChoice:
    """An area a place of the text can be: from the named areas, a request before, the coordinates or a geocoder."""
    #: The place as the text has it (or the coordinates).
    place: str
    #: As shown: 「横須賀港（地名テーブル・港湾）」.
    label: str
    rings: list
    #: 範囲 or 点 (as a request's shape).
    kind: str
    source: str


NAMED, BEFORE, WRITTEN, FOUND = "地名テーブル", "過去の依頼", "座標", "地名・住所検索"
#: The most choices for one place.
CHOICES = 5


def area_choices(word: str, areas: Sequence[Any], requests: Sequence[ImagingRequest] = ()) -> list[AreaChoice]:
    """
    The areas a place name can be: the named areas of that name (then those
    whose name holds it or that it holds, the smaller first), then the
    requests before whose name holds it (the latest first; one area once).
    """
    w = fold(word)
    if not w:
        return []
    out: list[AreaChoice] = []
    def names(a: Any) -> list[str]:
        return [fold(n) for n in (a.name, getattr(a, "name_en", "")) if n]

    same = [a for a in areas if w in names(a)]
    near = sorted((a for a in areas if a not in same and any(w in n or (len(n) >= 2 and n in w) for n in names(a))),
                  key=lambda a: (getattr(a, "size", 0.0) or 0.0, len(a.name)))
    for a in [*same, *near]:
        kind = getattr(a, "kind", "")
        out.append(AreaChoice(word, f"{a.name}（{NAMED}{'・' + kind if kind else ''}）", [list(r) for r in a.rings], "範囲", NAMED))
    seen: list = []
    for r in sorted((r for r in requests if r.rings and w in fold(r.name)), key=lambda r: -(r.updated or r.created or 0)):
        if r.rings in seen:
            continue
        seen.append(r.rings)
        when = jst_day(r.updated or r.created).strftime("%m/%d") if (r.updated or r.created) else ""
        out.append(AreaChoice(word, f"{r.name}（{BEFORE}{' ' + when if when else ''}）", [list(x) for x in r.rings],
                              r.shape_kind or "範囲", BEFORE))
    return out[:CHOICES]


def point_choice(lon: float, lat: float, km: float) -> AreaChoice:
    """A point written in the text, as a square of `km` (the form's 点 size)."""
    text = f"{abs(lat):.4f}{'N' if lat >= 0 else 'S'} {abs(lon):.4f}{'E' if lon >= 0 else 'W'}"
    return AreaChoice(text, f"{text}（{WRITTEN}・一辺 {km:g} km）", square_rings(lon, lat, km), "点", WRITTEN)


def found_choice(word: str, place: Any, km: float) -> AreaChoice:
    """A place a geocoder found (core/geocode.Place), as a square of `km` around it."""
    lon, lat = place.lon_lat
    return AreaChoice(word, f"{place.name}（{FOUND}・{place.source}・一辺 {km:g} km）", square_rings(lon, lat, km), "点", FOUND)


def batch_names(d: "RequestDraft", places: Sequence[str]) -> list[str]:
    """
    The names of the requests made one for each place: the draft's name
    with its place changed to each place (「横須賀港 光学」 → 「佐世保港 光学」)
    when it names one of them; the place before a name that names none; the
    place with the 種別 when it names several (or there is no name).
    """
    name = d.values.get("name", "")
    kind = d.values.get("kind")
    plain = [(p + (f" {kind}" if kind and kind != "任意" else "")) for p in places]
    held = [p for p in dict.fromkeys(places) if p and p in name]
    if not name or len(held) > 1:
        return [n[:NAME_LIMIT] for n in plain]
    if held:
        return [name.replace(held[0], p, 1)[:NAME_LIMIT] for p in places]
    return [f"{p} {name}"[:NAME_LIMIT] for p in places]


# ---- the draft ------------------------------------------------------------------------------------------


@dataclass
class RequestDraft:
    """The items read from a text (key → value; see ITEMS), the words each was read from, the places, and what to check."""
    values: dict = field(default_factory=dict)
    #: The words of the text an item was read from (「至急」), shown with it when they differ from the value.
    why: dict = field(default_factory=dict)
    #: Place names written, in order (to be looked up; the model never makes an area).
    places: list = field(default_factory=list)
    #: Coordinates written (lon, lat), in order.
    points: list = field(default_factory=list)
    #: Whether the text asks to change the area (editing).
    area_asked: bool = True
    #: What was left out or is unsure (確認).
    checks: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.values or self.places or self.points)

    def text_of(self, key: str) -> str:
        """An item as shown: 「期間: 2026-11-23（月）〜 2026-12-06（日）」."""
        return f"{LABELS[key]}: {value_text(key, self.values[key])}" + (
            f"（「{self.why[key]}」から）" if self.why.get(key) and self.why[key] != value_text(key, self.values[key]) else "")


def _day_text(d: Optional[date]) -> str:
    return f"{d.isoformat()}（{WEEKDAYS[d.weekday()]}）" if d else ""


def value_text(key: str, v: Any) -> str:
    """An item's value in words."""
    if key == "period":
        start, end = v
        return f"{_day_text(start)} 〜 {_day_text(end)}".strip() if start or end else ""
    if key == "repeat":
        r = repeat_of(v)
        return f"曜日 {'・'.join(WEEKDAYS[d] for d in sorted(r.weekdays))}" if r.kind == "曜日" else (
            f"{r.every} 日ごと" if r.kind == "間隔" else "毎日")
    if key == "times":
        n, m = v
        return f"{n}〜{m} 回" if m else f"{n} 回以上"
    if key == "priority":
        return next((f"{name}（{v}）" for name, low, high, _v in PRIORITY_LEVELS if low <= v <= high), str(v))
    if key in ("off_nadir", "incidence"):
        low, high = v
        return f"{low:g}〜{high:g}°" if low is not None and high is not None else (f"{low:g}° 以上" if low is not None else f"{high:g}° 以下")
    if key == "resolution":
        return f"{v:g} m 以下"
    if key == "cloud_no_go":
        return f"{v:g}%（{v:g}% 以上なら実施しない）"
    if key == "satellites":
        return v.text
    return str(v)


def request_messages(task: TextTask, text: str, today: date, request: Optional[ImagingRequest] = None) -> list[Message]:
    """The conversation that asks for a request's lines; `request`: the one being edited (only changes are asked for)."""
    hints = "、".join(f"{name} {a.isoformat()}" if a == b else f"{name} {a.isoformat()} 〜 {b.isoformat()}"
                     for name, a, b in future_hints(today))
    lines = [f"今日: {_day_text(today)}", f"日付の目安: {hints}"]
    if request is not None:
        start = jst_day(request.start) if math.isfinite(request.start) else None
        end = jst_day(request.end) if math.isfinite(request.end) else None
        lines.append(f"直す依頼: {request.name}（期間 {start.isoformat() if start else ''} 〜 {end.isoformat() if end else ''}）")
        lines.append("直す項目だけを書き、ほかは「なし」と書いてください。")
    lines.append(f"依頼の文: {text.strip()[:TEXT_LIMIT]}")
    out: list[Message] = [{"role": "system", "content": task.prompt}]
    for given, answer in task.examples:
        out += [{"role": "user", "content": f"今日: {_day_text(today)}\n依頼の文: {given}"}, {"role": "assistant", "content": answer}]
    out.append({"role": "user", "content": "\n".join(lines)})
    return out


def _has_words(value: str, text: str) -> bool:
    """Whether every word of `value` (split at spaces) is in the text (folded)."""
    t = fold(text)
    words = [w for w in re.split(r"[\s　]+", fold(value)) if w]
    return bool(words) and all(w in t for w in words)


def _foreign(value: str, text: str) -> list[str]:
    have = numbers_in(text)
    return [n for n in numbers_in(value) if not any(_same(n, h) for h in have)]


def _same(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _model_dates(value: str) -> Period:
    from .text_tasks import _dates_of

    start, end = _dates_of(value)
    return Period(start, end, bare(value))


def request_draft(raw: str, text: str, today: date, sats: Sequence[Any] = (), place_names: Sequence[str] = (),
                  request: Optional[ImagingRequest] = None) -> RequestDraft:
    """
    What is proposed from a text and the model's lines (`raw`): what the
    words name, then the model's values that the words give a footing.
    `place_names`: names to find in the text as they are (named areas);
    `request`: the request being edited (then only what the text names and
    what differs from it).
    """
    got = answer_lines(raw, REQUEST_KEYS) if raw else {}
    d = RequestDraft()
    t = _nfkc(text)
    editing = request is not None

    # 名称
    if not editing or _CHANGE_NAME.search(t):
        name = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", bare(got.get("名称", "")))).strip(" 「」『』")
        if name:
            foreign = _foreign(name, text)
            if foreign:
                d.checks.append(f"名称「{name}」の数字 {'・'.join(foreign)} は文にない（入れず）")
            elif len(name) > NAME_LIMIT:
                d.values["name"] = name[:NAME_LIMIT].rstrip()
                d.checks.append(f"名称は {NAME_LIMIT} 文字までに")
            else:
                d.values["name"] = name
    for key, label in (("management_no", "管理番号"), ("identification_no", "識別番号")):
        found = number_of(label, text)
        if found:
            d.values[key], d.why[key] = found
    if editing:
        if _RESTART.search(t):
            d.values["status"], d.why["status"] = "有効", "再開"
        elif _STOP.search(t):
            d.values["status"], d.why["status"] = "停止", _STOP.search(t)[0]

    # 期間
    period = period_of(text, today)
    extended = extension_of(text, jst_day(request.end) if editing and math.isfinite(request.end) else None)
    if extended is not None and period.end is None:
        period = Period(period.start, extended, _EXTEND.search(t)[0])
    if period.start or period.end:
        d.values["period"], d.why["period"] = (period.start, period.end), period.words
    elif "期間" in got:
        said = _model_dates(got["期間"])
        if (said.start or said.end) and DATE_CUE.search(t):
            d.values["period"] = (said.start, said.end)
            d.checks.append("期間は文から読み取れず、モデルの読み（日付を確かめて）")
        elif said.start or said.end:
            d.checks.append(f"期間「{said.words}」は文に手がかりなし（入れず）")

    # 頻度 → 区分・達成条件・繰り返し
    often = often_of(text) or (_often_of_model(got["頻度"], text) if "頻度" in got else None)
    if "頻度" in got and often is None and bare(got["頻度"]) not in ("", "なし"):
        d.checks.append(f"頻度「{bare(got['頻度'])}」は文に手がかりなし（入れず）")
    steady, one_off = _STEADY.search(t), _ONE_OFF.search(t)
    if often is not None:
        if often.kind in ("曜日", "間隔"):
            d.values.update(category="定点", achieve="毎回", repeat=often.text)
        elif often.kind == "毎日":
            if steady and not one_off:
                d.values.update(category="定点", achieve="毎回", repeat="毎日")
            else:
                d.values.update(category="臨時", achieve="毎回")
        else:
            d.values.update(category="臨時", achieve="期間内1回")
        for key in ("category", "achieve", "repeat"):
            if key in d.values:
                d.why[key] = often.words
    elif steady and not one_off:
        d.values["category"], d.why["category"] = "定点", steady[0]
    elif one_off and not steady:
        d.values["category"], d.why["category"] = "臨時", one_off[0]

    times = times_of(text)
    if times is None and "1日の回数" in got:
        n = numbers_in(bare(got["1日の回数"]))
        if n and n[0].isdigit() and 2 <= int(n[0]) <= 24:
            if re.search(r"(?:1日|一日|日に)[^。]{0,6}" + n[0] + r"\s*回", t):
                times = (int(n[0]), None, got["1日の回数"])
            else:
                d.checks.append(f"1 日の回数「{bare(got['1日の回数'])}」は文に手がかりなし（入れず）")
    if times is not None:
        d.values["times"], d.why["times"] = (times[0], times[1]), times[2]

    # 優先度
    p = priority_of(text)
    if p is not None:
        d.values["priority"], d.why["priority"] = p
    elif "優先度" in got and bare(got["優先度"]) not in ("", "なし"):
        d.checks.append(f"優先度「{bare(got['優先度'])}」は文に手がかりなし（入れず）")

    # 条件
    kind = kind_of(text)
    if kind:
        d.values["kind"] = kind
    look = look_of(text)
    if look:
        d.values["look_side"] = look
    orbit = orbit_of(text)
    if orbit:
        d.values["orbit_dir"] = orbit
    tod = time_of_day_of(text)
    if tod:
        d.values["time_of_day"] = tod
    if sats:
        named = satellites_of(text, sats)
        if not named.empty:
            d.values["satellites"] = named
    for key, which, label in (("off_nadir", OFF_NADIR, "オフナディア角"), ("incidence", INCIDENCE, "入射角")):
        found = angles_of(text, which)
        if found:
            d.values[key], d.why[key] = (found[0], found[1]), found[2]
        elif label in got and bare(got[label]) not in ("", "なし"):
            d.checks.append(f"{label}「{bare(got[label])}」は文で読み取れず（入れず）")
    res = resolution_of(text)
    if res:
        d.values["resolution"], d.why["resolution"] = res
    elif "分解能" in got and bare(got["分解能"]) not in ("", "なし"):
        d.checks.append(f"分解能「{bare(got['分解能'])}」は文で読み取れず（入れず）")
    cloud = cloud_of(text)
    if cloud is None and "雲量" in got and bare(got["雲量"]) not in ("", "なし"):
        said = cloud_of("雲量" + bare(got["雲量"]).removeprefix("雲量"))
        if said and not _foreign(bare(got["雲量"]), text) and re.search(r"雲", t):
            cloud = said
            d.checks.append("実施しない雲量はモデルの読み（確かめて）")
        else:
            d.checks.append(f"雲量「{bare(got['雲量'])}」は文で読み取れず（入れず）")
    if cloud:
        d.values["cloud_no_go"], d.why["cloud_no_go"] = cloud
        if kind == "SAR":
            d.checks.append("SAR の依頼に雲量（SAR には効かない）")

    # 依頼者
    if not editing or _CHANGE_REQUESTER.search(t):
        who = bare(got.get("依頼者", ""))
        if who and _has_words(who, text) and len(who) <= 40:
            d.values["requester"] = who
        else:
            m = _REQUESTER.search(t)
            if m:
                d.values["requester"], d.why["requester"] = m[1].strip(), m[0]
            elif who:
                d.checks.append(f"依頼者「{who}」は文にない（入れず）")

    # 地名
    d.area_asked = not editing or bool(_CHANGE_AREA.search(t))
    if d.area_asked:
        d.points = points_of(text)
        names = places_in(text, place_names)
        model = [bare(w) for w in re.split(r"[、,，・/]|および|と(?=[^\s]{2,})", got.get("地名", "")) if bare(w)]
        for w in model:
            if fold(w) in fold(text) and _place_word(w):
                if not any(fold(w) in fold(n) or fold(n) in fold(w) for n in names):
                    names.append(w)
            elif fold(w) not in ("なし", "不明"):
                d.checks.append(f"地名「{w}」は文にない（使わず）")
        d.places = sorted(names, key=lambda n: fold(text).find(fold(n)))
        if "name" not in d.values and not editing and names:
            d.values["name"] = (names[0] + (f" {kind}" if kind and kind != "任意" else ""))[:NAME_LIMIT]
    if editing:
        d = changes(d, request)
    return d


def _request_value(r: ImagingRequest, key: str) -> Any:
    if key == "period":
        return (jst_day(r.start) if math.isfinite(r.start) else None, jst_day(r.end) if math.isfinite(r.end) else None)
    if key == "times":
        return (int(r.times_min or 1), r.times_max)
    if key == "off_nadir":
        return (r.off_nadir_min, r.off_nadir_max)
    if key == "incidence":
        return (r.incidence_min, r.incidence_max)
    if key == "satellites":
        return Satellites(tuple(r.series), tuple(r.satellites), tuple(r.modes))
    if key == "look_side":
        return r.look_side
    if key == "orbit_dir":
        return r.orbit_dir
    if key == "cloud_no_go":
        return r.cloud_no_go
    return getattr(r, key, None)


def changes(d: RequestDraft, r: ImagingRequest) -> RequestDraft:
    """The draft without what `r` already has (editing): a side of the period not named stays as it is."""
    out = replace(d, values={}, why=dict(d.why))
    for key, v in d.values.items():
        now = _request_value(r, key)
        if key == "period":
            v = (v[0] if v[0] is not None and v[0] != now[0] else None, v[1] if v[1] is not None and v[1] != now[1] else None)
            if v == (None, None):
                continue
        elif key in ("off_nadir", "incidence"):
            v = (v[0] if v[0] != now[0] else None, v[1] if v[1] != now[1] else None)
            if v == (None, None):
                continue
        elif key == "satellites":
            if (fold("・".join(v.series)), fold("・".join(v.satellites)), fold("・".join(v.modes))) == (
                    fold("・".join(now.series)), fold("・".join(now.satellites)), fold("・".join(now.modes))):
                continue
        elif key == "priority":
            if v == r.priority:
                continue
        elif key == "repeat":
            if repeat_of(v) == repeat_of(r.repeat):
                continue
        elif v == now:
            continue
        out.values[key] = v
    return out


def applied(r: ImagingRequest, d: RequestDraft, keys: Sequence[str], text: str = "", note: bool = True,
            now: Optional[datetime] = None) -> ImagingRequest:
    """
    `r` with the items `keys` of the draft (each as the form keeps it); a
    side of a period, an angle or the times not named stays as it was. With
    `note`, the text is added to the request's メモ (where it came from).
    """
    out = ImagingRequest(**{**r.__dict__})
    v = d.values
    for key in keys:
        if key not in v:
            continue
        value = v[key]
        if key == "period":
            start, end = value
            if start is not None:
                out.start = day_bounds(start)[0]
            if end is not None:
                out.end = day_bounds(end)[1]
        elif key == "times":
            out.times_min, out.times_max = value[0], value[1]
        elif key == "off_nadir":
            if value[0] is not None:
                out.off_nadir_min = value[0]
            if value[1] is not None:
                out.off_nadir_max = value[1]
        elif key == "incidence":
            if value[0] is not None:
                out.incidence_min = value[0]
            if value[1] is not None:
                out.incidence_max = value[1]
        elif key == "satellites":
            out.series, out.satellites, out.modes = list(value.series), list(value.satellites), list(value.modes)
        elif key == "priority":
            out.priority = int(value)
        else:
            setattr(out, key, value)
    if note and text.strip():
        stamp = (now or datetime.now()).strftime("%Y-%m-%d")
        one_line = re.sub(r"\s+", " ", text.strip())
        added = f"依頼文（{stamp}）: {one_line}"
        out.note = f"{out.note}\n{added}" if out.note.strip() else added
    return out
