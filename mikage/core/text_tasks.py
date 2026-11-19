"""
文章の手伝い for a model of about 3B (core/text_assist.py; tsudo 2026-11-19:
「3Bに対応しつつ、コンフィグでできる範囲を規定」 → 「この方針で4つとも」):
what the model is asked, and what is kept of its answer. Each gives a
proposal only; the screens change nothing until the person takes it.

- 項目の提案 (noteFields): the record's 種別・活動・確度・長さ・幅・向き・モノ
  picked from its 備考. Only the choices given are kept (the classification's
  kinds, the kind's activities, the 確度 levels); a number not written in
  the 備考 and a name it does not contain are dropped.
- 言葉で探す (searchWords): the export's conditions (期間・地名・大分類・確度・
  有無: あり・消失・不可) from a sentence. The model is given the dates of 「今日」「先週」… so it
  only copies them; places, classes and levels not among the choices are
  dropped.
- まとめ (summary): a few sentences on records (their counts given first, so
  the model need not count); numbers the records do not have are pointed out.
- 英訳 (translate): a 備考 in English (the 通報's `note.en`).

The model answers in lines 「項目: 値」 (easier for a small model than JSON).
Pure Python; the screens are ui/text_assist_ui.py.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from .obs.model import unseen_label
from .text_assist import Draft, Message, TextTask, clean_note, clean_paragraph, foreign_numbers, numbers_in

#: Written by the model for a value it does not have.
NONE_WORDS = {"", "なし", "無し", "不明", "-", "－", "ー", "—", "空", "none", "null", "n/a", "na", "未記入", "該当なし"}
#: The most kinds named to the model as choices (the current 大分類's first).
KINDS_LIMIT = 200
#: The most of the records sent for まとめ (characters; the rest is cut, the counts come first).
SUMMARY_LIMIT = 3000
#: The most records listed for まとめ (the counts cover them all).
SUMMARY_RECORDS = 80
WEEKDAYS = "月火水木金土日"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


def answer_lines(raw: str, keys: Sequence[str]) -> dict[str, str]:
    """The model's 「項目: 値」 lines for `keys` (the first of each; a value of 「なし」 left out)."""
    out: dict[str, str] = {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = re.sub(r"^[\s\-*・#]+", "", line.strip().strip("`"))
        m = re.match(r"^([^:：]{1,12})\s*[:：]\s*(.*)$", line)
        if not m:
            continue
        key, value = m[1].strip(), m[2].strip().strip("「」『』\"'").strip()
        if key in keys and key not in out and _fold(value) not in NONE_WORDS:
            out[key] = value
    return out


def _number(text: str) -> Optional[str]:
    """The first number of a value (「約 120 m」 → 120), as written."""
    found = numbers_in(text)
    return found[0] if found else None


# ---- 項目の提案 ----------------------------------------------------------------------------------

FIELD_KEYS = ("種別", "活動", "確度", "長さ", "幅", "向き", "モノ")


@dataclass(frozen=True)
class FieldsDraft:
    """The fields picked from a 備考 (only those kept), and what was dropped and why."""
    #: kind (its path), activity, confidence, length, width, heading (numbers as float), object (a name).
    values: dict = field(default_factory=dict)
    dropped: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        names = {"kind": "種別", "activity": "活動", "confidence": "確度", "length": "長さ", "width": "幅", "heading": "向き",
                 "object": "モノ"}
        units = {"length": " m", "width": " m", "heading": "°"}
        parts = []
        for k, v in self.values.items():
            shown = " > ".join(v) if k == "kind" else f"{v:g}" if isinstance(v, float) else str(v)
            parts.append(f"{names[k]} {shown}{units.get(k, '')}")
        return "・".join(parts)


def kinds_for(kinds: Sequence[str], root: str = "", limit: int = KINDS_LIMIT) -> list[str]:
    """The kinds named to the model: those of `root` first (shallow before deep), up to `limit`."""
    def depth(k: str) -> int:
        return k.count(">")

    mine = [k for k in kinds if root and (k == root or k.startswith(root + " >"))]
    rest = [k for k in kinds if k not in mine]
    return list(dict.fromkeys(sorted(mine, key=depth) + sorted(rest, key=depth)))[:limit]


def fields_messages(task: TextTask, note: str, kind: str, kinds: Sequence[str], activities: dict[str, list[str]],
                    confidences: Sequence[str]) -> list[Message]:
    """
    The conversation that asks for the fields of `note`: the kind written so
    far, the kinds, each 大分類's activities and the 確度 levels to choose from.
    """
    lines = []
    if kind.strip():
        lines.append(f"今の種別: {kind.strip()}")
    lines.append("種別の候補: " + "、".join(kinds))
    for root, words in activities.items():
        if words:
            lines.append(f"活動の候補（{root}）: " + "、".join(words))
    if confidences:
        lines.append("確度の候補: " + "、".join(confidences))
    lines.append(f"備考: {note.strip()[:400]}")
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": "\n".join(lines)}]


def fields_draft(raw: str, note: str, resolve_kind: Callable[[str], Optional[list[str]]],
                 activities_of: Callable[[str], list[str]], confidences: Sequence[str], kind_now: str = "") -> FieldsDraft:
    """
    What is kept of the model's fields: a kind of the classification
    (`resolve_kind`: a path, or a class's name → its path), an activity of
    that kind (`activities_of`), a 確度 among `confidences`, numbers written
    in `note`, and a name `note` contains.
    """
    got = answer_lines(raw, FIELD_KEYS)
    values: dict = {}
    dropped: list[str] = []
    if "種別" in got:
        path = resolve_kind(got["種別"]) or resolve_kind(re.split(r"\s*[>＞]\s*", got["種別"])[-1])
        if path:
            values["kind"] = list(path)
        else:
            dropped.append(f"分類にない種別「{got['種別']}」")
    kind = " > ".join(values["kind"]) if "kind" in values else kind_now
    if "活動" in got:
        words = activities_of(kind)
        match = next((w for w in words if _fold(w) == _fold(got["活動"])), None)
        if match:
            values["activity"] = match
        else:
            dropped.append(f"{'この種別の' if kind else ''}活動にない「{got['活動']}」")
    if "確度" in got:
        match = next((c for c in confidences if _fold(c) == _fold(got["確度"])), None)
        if match:
            values["confidence"] = match
        else:
            dropped.append(f"確度にない「{got['確度']}」")
    have = numbers_in(note)
    for key, name, high in (("長さ", "length", 2000.0), ("幅", "width", 500.0), ("向き", "heading", 360.0)):
        if key not in got:
            continue
        n = _number(got[key])
        if n is None:
            dropped.append(f"{key}「{got[key]}」は数字でない")
        elif not any(_same(n, h) for h in have):
            dropped.append(f"{key} {n} は備考にない数字")
        elif not 0 <= float(n) <= high:
            dropped.append(f"{key} {n} は範囲外")
        else:
            values[name] = float(n)
    if "モノ" in got:
        if _fold(got["モノ"]) and _fold(got["モノ"]) in _fold(note):
            values["object"] = got["モノ"]
        else:
            dropped.append(f"備考にない名前「{got['モノ']}」")
    return FieldsDraft(values, tuple(dropped))


def _same(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


# ---- 言葉で探す ---------------------------------------------------------------------------------

SEARCH_KEYS = ("期間", "地名", "大分類", "確度", "有無")


def date_hints(today: date) -> list[tuple[str, date, date]]:
    """The periods a request names most, as dates (今日, 昨日, 今週 = 月〜日 …)."""
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return [
        ("今日", today, today), ("昨日", today - timedelta(days=1), today - timedelta(days=1)),
        ("一昨日", today - timedelta(days=2), today - timedelta(days=2)),
        ("今週", monday, monday + timedelta(days=6)), ("先週", monday - timedelta(days=7), monday - timedelta(days=1)),
        ("直近 7 日", today - timedelta(days=6), today), ("直近 30 日", today - timedelta(days=29), today),
        ("今月", first, next_month - timedelta(days=1)), ("先月", last_month_end.replace(day=1), last_month_end),
        ("今年", today.replace(month=1, day=1), today.replace(month=12, day=31)),
    ]


def _day(d: date) -> str:
    return f"{d.isoformat()}（{WEEKDAYS[d.weekday()]}）"


def search_messages(task: TextTask, words: str, today: date, places: Sequence[str], roots: Sequence[str],
                    confidences: Sequence[str]) -> list[Message]:
    """The conversation that turns `words` into the export's conditions."""
    lines = [f"今日: {_day(today)}", "日付の目安: " + "、".join(
        f"{name} {a.isoformat()}" if a == b else f"{name} {a.isoformat()} 〜 {b.isoformat()}" for name, a, b in date_hints(today))]
    if places:
        lines.append("地名の候補: " + "、".join(places[:300]))
    if roots:
        lines.append("大分類の候補: " + "、".join(roots))
    if confidences:
        lines.append("確度の候補: " + "、".join(confidences))
    lines.append(f"依頼: {words.strip()[:300]}")
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": "\n".join(lines)}]


@dataclass(frozen=True)
class SearchDraft:
    """The conditions read from a request (None: not said), and what was dropped."""
    start: Optional[date] = None
    end: Optional[date] = None
    place: Optional[str] = None
    roots: tuple[str, ...] = ()
    confidence: Optional[str] = None
    #: "present", "absent", "unseen" (不可) or None (the export's 有無).
    presence: Optional[str] = None
    dropped: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.start or self.end or self.place or self.roots or self.confidence or self.presence)

    @property
    def text(self) -> str:
        parts = []
        if self.start or self.end:
            parts.append(f"期間 {self.start.isoformat() if self.start else ''}〜{self.end.isoformat() if self.end else ''}")
        if self.place:
            parts.append(f"地名 {self.place}")
        if self.roots:
            parts.append(f"大分類 {'・'.join(self.roots)}")
        if self.confidence:
            parts.append(f"確度 {self.confidence}")
        if self.presence:
            parts.append("有無 " + {"present": "ありのみ", "absent": "なしのみ", "unseen": "不可のみ"}[self.presence])
        return "・".join(parts)


_DATE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")


def _dates_of(text: str) -> list[Optional[date]]:
    """The two sides of 「a 〜 b」 (one side may be empty); a single date is both."""
    t = unicodedata.normalize("NFKC", text)
    sides = re.split(r"\s*[〜~～]\s*|\s+-\s+|\s*から\s*", t, maxsplit=1)

    def one(s: str) -> Optional[date]:
        m = _DATE.search(s)
        if not m:
            return None
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    if len(sides) == 1:
        d = one(sides[0])
        return [d, d]
    return [one(sides[0]), one(sides[1])]


def search_draft(raw: str, places: Sequence[str], roots: Sequence[str], confidences: Sequence[str]) -> SearchDraft:
    """What is kept of the model's conditions: dates read, a place, 大分類 and 確度 among the choices."""
    got = answer_lines(raw, SEARCH_KEYS)
    dropped: list[str] = []
    start = end = None
    if "期間" in got:
        start, end = _dates_of(got["期間"])
        if start is None and end is None:
            dropped.append(f"日付として読めない期間「{got['期間']}」")
        elif start and end and start > end:
            start, end = end, start
    place = None
    if "地名" in got:
        place = next((p for p in places if _fold(p) == _fold(got["地名"])), None)
        if place is None:
            dropped.append(f"地名の候補にない「{got['地名']}」")
    chosen: list[str] = []
    if "大分類" in got:
        for word in re.split(r"[、,・/\s]+", got["大分類"]):
            match = next((r for r in roots if _fold(r) == _fold(word)), None)
            if match:
                chosen.append(match)
            elif word.strip():
                dropped.append(f"大分類にない「{word}」")
    confidence = None
    if "確度" in got:
        word = re.sub(r"\s*以上$", "", got["確度"])
        confidence = next((c for c in confidences if _fold(c) == _fold(word)), None)
        if confidence is None:
            dropped.append(f"確度にない「{got['確度']}」")
    presence = None
    if "有無" in got:
        word = _fold(got["有無"])
        presence = ("present" if word.startswith(("あり", "在")) else "absent" if word.startswith(("消失", "不在", "いない"))
                    else "unseen" if word.startswith(("不可", "見えない")) else None)
        if presence is None:
            dropped.append(f"有無にない「{got['有無']}」")
    return SearchDraft(start, end, place, tuple(dict.fromkeys(chosen)), confidence, presence, tuple(dropped))


# ---- まとめ ----------------------------------------------------------------------------------------

def _root(kind: str) -> str:
    return re.split(r"\s*>\s*", kind.strip())[0] if kind.strip() else "種別なし"


def summary_source(records: Iterable, when: Callable[[Optional[float]], str], limit: int = SUMMARY_LIMIT,
                   most: int = SUMMARY_RECORDS) -> str:
    """The records for まとめ: their counts by 大分類 and activity, then one line each (image time order), cut to `limit`."""
    rows = sorted(records, key=lambda o: (o.image_time or 0, o.created_at))
    present = [o for o in rows if o.present]
    unseen = [o for o in rows if getattr(o, "unseen", "")]
    gone = len(rows) - len(present) - len(unseen)
    lines = [f"記録: {len(rows)} 件（あり {len(present)}・消失 {gone}" + (f"・不可 {len(unseen)}" if unseen else "") + "）"]
    by_root = Counter(_root(o.kind) for o in present)
    for root, n in by_root.most_common():
        acts = Counter(o.activity or "活動なし" for o in present if _root(o.kind) == root)
        lines.append(f"{root}: {n} 件（" + "・".join(f"{a} {m}" for a, m in acts.most_common()) + "）")
    images = list(dict.fromkeys(o.image_name for o in rows if o.image_name))
    if images:
        lines.append(f"画像: {len(images)} 枚（" + "・".join(images[:5]) + ("…" if len(images) > 5 else "") + "）")
    lines.append("")
    for o in rows[:most]:
        cells = [when(o.image_time), o.kind or "種別なし", o.object_name, o.activity, o.confidence,
                 "" if o.present else unseen_label(o.unseen) if getattr(o, "unseen", "") else "消失",
                 f"長さ {o.length:g} m" if o.length else "", o.note]
        lines.append(" / ".join(c for c in cells if c))
    if len(rows) > most:
        lines.append(f"…（ほか {len(rows) - most} 件）")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…（以下略）"
    return text


def summary_messages(task: TextTask, source: str) -> list[Message]:
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": "観測記録:\n" + source}]


def summary_draft(raw: str, source: str) -> Draft:
    """The model's まとめ, and the numbers in it that the records do not have."""
    text = clean_paragraph(raw)
    return Draft(text, foreign_numbers(source, text))


# ---- 英訳 ----------------------------------------------------------------------------------------

def translate_messages(task: TextTask, note: str) -> list[Message]:
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": f"Note: {note.strip()[:400]}"}]


def translated(raw: str, note: str) -> Draft:
    """The model's English (one line, without a label), and the numbers in it the 備考 does not have."""
    text = re.sub(r"^(?:English|Translation|Note)\s*:\s*", "", clean_note(raw), flags=re.IGNORECASE).strip()
    return Draft(text, foreign_numbers(note, text))


def today_of(now_ms: float) -> date:
    """The local date of a moment (ms)."""
    return datetime.fromtimestamp(now_ms / 1000).date()
