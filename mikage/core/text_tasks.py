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
Tried on sarashina2.2 1B and 3B (2026-11-20), a small model also writes why
it has no value (「備考に記載がないため「なし」」), adds 「（または…）」, and
fills in what was not asked (a 確度, an 有無, a place). So: what it says of
a value is taken off, what the words themselves name wins (a place, a
class, a period, how sure, 雲 → 不可), and the model's value is kept only
where the words give it a footing. It stops once its lines are written.

Pure Python; the screens are ui/text_assist_ui.py.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from .obs.api import unseen_label
from .text_assist import (Draft, Message, TextTask, _kanji_value, _unquoted, clean_note, clean_paragraph, foreign_letters,
                          foreign_numbers, numbers_in)

#: Written by the model for a value it does not have.
NONE_WORDS = {"", "なし", "無し", "不明", "-", "－", "ー", "—", "空", "none", "null", "n/a", "na", "未記入", "該当なし"}
#: The most kinds named to the model as choices (those the 備考 names first, then the current 大分類's).
KINDS_LIMIT = 60
#: The most of the records sent for まとめ (characters; the rest is cut, the counts come first).
SUMMARY_LIMIT = 3000
#: The most records listed for まとめ (the counts cover them all).
SUMMARY_RECORDS = 80
WEEKDAYS = "月火水木金土日"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().lower()


#: Written by the model instead of a value, to say why it has none (「備考に記載がないため「なし」」).
_EXPLAINED = re.compile(r"[「『\"](?:なし|無し|不明)[」』\"]|記載|ないため|がないので|特定の|わから|分から|判断でき|推測可能")
_PAREN = re.compile(r"[（(](?:[^（）()]|[（(][^（）()]*[）)])*[）)]")
_OR = re.compile(r"\s*(?:または|もしくは|あるいは|\bor\b)\s*", re.IGNORECASE)
_KEY_LINE = re.compile(r"^([^:：]{1,12})\s*[:：]\s*(.*)$")


def bare(value: str) -> str:
    """A value without what the model said of it: 「補給艦（または…）」 → 補給艦, 「消失 または 不可」 → 消失."""
    v = _OR.split(_PAREN.sub("", value))[0]
    return v.strip(" \t　、。,.").strip("「」『』\"'").strip()


def _key_line(line: str) -> Optional[tuple[str, str]]:
    m = _KEY_LINE.match(re.sub(r"^[\s\-*・#]+", "", line.strip().strip("`")))
    return (m[1].strip(), m[2].strip().strip("「」『』\"'").strip()) if m else None


def answer_lines(raw: str, keys: Sequence[str]) -> dict[str, str]:
    """
    The model's 「項目: 値」 lines for `keys` (the first of each, as written);
    a value of 「なし」 and one that only says why there is none left out.
    """
    out: dict[str, str] = {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        got = _key_line(line)
        if got is None:
            continue
        key, value = got
        if key in keys and key not in out and _fold(bare(value)) not in NONE_WORDS and not _EXPLAINED.search(value):
            out[key] = value
    return out


def lines_done(keys: Sequence[str]) -> Callable[[str], bool]:
    """
    Whether the model has written its 「項目: 値」 lines (each of `keys`, a
    key again, or other words after them such as an explanation): it stops
    there instead of writing on to its token limit.
    """
    wanted = set(keys)

    def done(text: str) -> bool:
        seen: set[str] = set()
        for line in text.replace("\r\n", "\n").split("\n")[:-1]:
            got = _key_line(line)
            if got is not None and got[0] in wanted:
                if got[0] in seen:
                    return True
                seen.add(got[0])
                if seen == wanted:
                    return True
            elif line.strip() and seen:
                return True
        return False

    return done


def _leaf(kind: str) -> str:
    return re.split(r"\s*>\s*", kind.strip())[-1]


def _named(word: str, folded: str) -> bool:
    """Whether `word` (2 characters or more, not a 〇〇 placeholder) is in the folded text."""
    w = _fold(word)
    return len(w) >= 2 and "〇〇" not in w and "○○" not in w and w in folded


def kinds_in(text: str, kinds: Sequence[str], root: str = "") -> list[str]:
    """The kinds whose class name `text` contains (those of `root` first, then the finer, then the longer name)."""
    t = _fold(text)
    found = [k for k in dict.fromkeys(kinds) if _named(_leaf(k), t)]
    return sorted(found, key=lambda k: (0 if root and _root(k) == root else 1, -k.count(">"), -len(_leaf(k))))


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


def kinds_for(kinds: Sequence[str], root: str = "", limit: int = KINDS_LIMIT, note: str = "") -> list[str]:
    """The kinds named to the model: those `note` names first, then those of `root` (shallow before deep), up to `limit`."""
    def depth(k: str) -> int:
        return k.count(">")

    named = kinds_in(note, kinds, root) if note else []
    mine = [k for k in kinds if root and (k == root or k.startswith(root + " >"))]
    rest = [k for k in kinds if k not in mine]
    return list(dict.fromkeys(named + sorted(mine, key=depth) + sorted(rest, key=depth)))[:limit]


def fields_messages(task: TextTask, note: str, kind: str, kinds: Sequence[str], activities: dict[str, list[str]],
                    confidences: Sequence[str]) -> list[Message]:
    """
    The conversation that asks for the fields of `note`: the kind written so
    far, the kinds, each 大分類's activities and the 確度 levels to choose from.
    """
    lines = []
    if kind.strip():
        lines.append(f"今の種別: {kind.strip()}")
    lines.append("種別の候補: " + "、".join(kinds_for(kinds, _root(kind) if kind.strip() else "", note=note)))
    for root, words in activities.items():
        if words:
            lines.append(f"活動の候補（{root}）: " + "、".join(words))
    if confidences:
        lines.append("確度の候補: " + "、".join(confidences))
    lines.append(f"備考: {note.strip()[:400]}")
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": "\n".join(lines)}]


#: Words of a 備考 that say how sure it is, for the usual 確度 levels (a level's own name counts too).
CONFIDENCE_CUES: dict[str, tuple[str, ...]] = {
    "確定": ("確定", "確実", "間違いな", "断定", "を確認", "確認済", "確認ずみ", "確認した"),
    "推定": ("推定", "たぶん", "多分", "おそらく", "恐らく", "っぽ", "らしい", "らしき", "模様", "もよう", "思われ", "思しき", "可能性",
           "見込", "推測", "かも", "みたい", "ようだ", "とみられ", "と見られ"),
    "不明": ("判別でき", "識別でき", "判読でき", "わからな", "分からな"),
}
#: Names of a kind of thing rather than of one thing (not a モノ).
GENERIC_NAMES = {"機体", "船", "艦", "艦艇", "船舶", "車", "車両", "建物", "物体", "目標", "航空機", "不明"}
#: A class's name rather than one thing's: kanji ending in 艦・船・機・艇・車 (「護衛艦」「貨物船」; 「おうみ」「能登丸」 are names).
_CLASS_WORD = re.compile(r"^[\u4e00-\u9fff々]{1,6}[艦船機艇車]$")
#: Directions in degrees (16 points), the longer first.
COMPASS = {"北北東": 22.5, "東北東": 67.5, "東南東": 112.5, "南南東": 157.5, "南南西": 202.5, "西南西": 247.5, "西北西": 292.5,
           "北北西": 337.5, "北東": 45.0, "南東": 135.0, "南西": 225.0, "北西": 315.0, "北": 0.0, "東": 90.0, "南": 180.0, "西": 270.0}
_POINTS = "|".join(sorted(COMPASS, key=len, reverse=True))
#: 「針路は南東」「方位: 北」「北上中」「南下」「北東へ」「西向き」: a heading (not 「北側」「東京湾」).
_HEADING_WORDS = re.compile(rf"(?:針路|進路|方位|向き|艦首|機首|船首|方向|方角)\s*(?:は|が|:|：|を)?\s*(?:およそ|約|ほぼ)?\s*({_POINTS})(?![側部端岸]|京)"
                            rf"|({_POINTS})(?:に向|へ|向き|方向に|進)|(北)(?=上)|(南)(?=下)")


def compass_in(text: str) -> Optional[tuple[float, str]]:
    """The heading `text` gives in words (「針路は南東」 → 135, 「北上中」 → 0), and those words; None when it gives none or two."""
    found = {(COMPASS[next(g for g in m.groups() if g)], m[0]) for m in _HEADING_WORDS.finditer(unicodedata.normalize("NFKC", text))}
    if len({d for d, _w in found}) != 1:
        return None
    return min(found, key=lambda f: len(f[1]))


_AMOUNT = r"(\d+(?:\.\d+)?|[〇零一二三四五六七八九十百千]+)"
_METRES = re.compile(_AMOUNT + r"\s*(?:m|メートル)(?![a-z])", re.IGNORECASE)
_DEGREES = re.compile(_AMOUNT + r"\s*(?:度|°|゜)")


def _amounts(pattern: re.Pattern, text: str) -> list[tuple[int, str]]:
    """Where and what the numbers before a unit are (kanji numbers as digits)."""
    out = []
    for m in pattern.finditer(text):
        n = m[1] if m[1][0].isdigit() else _kanji_value(m[1])
        if n is not None:
            out.append((m.start(), str(n)))
    return out


def _cues(level: str) -> tuple[str, ...]:
    own = (level,) if len(level) >= 2 and level != "不明" else ()
    return CONFIDENCE_CUES.get(level, ()) + own + (f"確度{level}", f"確度は{level}", f"確度:{level}")


def confidence_in(text: str, confidences: Sequence[str]) -> Optional[str]:
    """The 確度 `text` says (the level's name, or a word like 「たぶん」), the first said; None when it says none."""
    t = _fold(text)
    best: Optional[tuple[int, str]] = None
    for c in confidences:
        at = min((t.find(_fold(w)) for w in _cues(c) if _fold(w) in t), default=-1)
        if at >= 0 and (best is None or at < best[0]):
            best = (at, c)
    return best[1] if best else None


def _any_cue(text: str) -> bool:
    t = _fold(text)
    return any(_fold(w) in t for words in CONFIDENCE_CUES.values() for w in words)


def _kind_words(value: str) -> list[str]:
    """What the model's 種別 may name: 「船舶（補給艦）」 → 船舶 > 補給艦, 補給艦, 船舶; 「A > B（…）」 → A > B, B."""
    out = [value.strip()]
    m = re.match(r"^(.+?)[（(]([^（）()]+)[）)]$", value.strip())
    if m:
        out += [f"{m[1].strip()} > {m[2].strip()}", m[2].strip(), m[1].strip()]
    b = bare(value)
    out += [b, re.split(r"\s*[>＞]\s*", b)[-1]]
    return [w for w in dict.fromkeys(out) if w]


_COUNTED = re.compile(r"\d+\s*(?:隻|機|台|両|基|棟|人|名|個|本)")


def _phrase(name: str, activities: Sequence[str]) -> bool:
    """Whether a モノ is part of the 備考 rather than a name (「2機が滑走路端で離陸準備のもよう」)."""
    return (len(name) > 24 or any(c in name for c in "、。，．") or bool(_COUNTED.search(name))
            or any(_named(a, _fold(name)) for a in activities))


def _ancestor(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether the kind `a` is `b` or contains it (船舶 of 船舶 > 補給艦)."""
    return len(a) <= len(b) and list(b[:len(a)]) == list(a)


def fields_draft(raw: str, note: str, resolve_kind: Callable[[str], Optional[list[str]]],
                 activities_of: Callable[[str], list[str]], confidences: Sequence[str], kind_now: str = "",
                 kinds: Sequence[str] = ()) -> FieldsDraft:
    """
    What is kept of the model's fields: a kind of the classification
    (`resolve_kind`: a path, or a class's name → its path; a finer class the
    note names among `kinds` wins), an activity of that kind
    (`activities_of`; one the 備考 names wins), a 確度 among `confidences`
    the 備考 gives a footing for, lengths in m and headings in degrees
    written in `note`, and a name `note` contains that is not a class's.
    """
    got = answer_lines(raw, FIELD_KEYS)
    values: dict = {}
    dropped: list[str] = []
    folded = _fold(note)
    model_path = None
    if "種別" in got:
        model_path = next((p for p in (resolve_kind(w) for w in _kind_words(got["種別"])) if p), None)
        if not model_path:
            dropped.append(f"分類にない種別「{bare(got['種別']) or got['種別']}」")
    named = kinds_in(note, kinds, _root(kind_now) if kind_now.strip() else "")
    note_path = next((p for p in (resolve_kind(k) for k in named) if p), None)
    path = model_path
    if note_path and (not model_path or _ancestor(model_path, note_path) or not _named(model_path[-1], folded)):
        # The class the 備考 names, rather than a coarser one or one it does not name.
        path = note_path
    if path:
        values["kind"] = list(path)
    kind = " > ".join(values["kind"]) if "kind" in values else kind_now
    words = activities_of(kind)
    activity = None
    if "活動" in got:
        activity = next((w for w in words if _fold(w) == _fold(bare(got["活動"]))), None)
    said = sorted((w for w in words if _named(w, folded)), key=len, reverse=True)
    if "活動" in got and activity is None and not said:
        dropped.append(f"{'この種別の' if kind else ''}活動にない「{bare(got['活動'])}」")
    if said and (activity is None or not _named(activity, folded)):
        activity = said[0]
    if activity:
        values["activity"] = activity
    said_confidence = confidence_in(note, confidences)
    confidence = None
    if "確度" in got:
        confidence = next((c for c in confidences if _fold(c) == _fold(bare(got["確度"]))), None)
        if confidence is None:
            dropped.append(f"確度にない「{bare(got['確度'])}」")
        elif said_confidence is None and (confidence in CONFIDENCE_CUES or not _any_cue(note)):
            dropped.append(f"確度「{confidence}」は備考に手がかりなし")
            confidence = None
    if said_confidence and (confidence is None or confidence in CONFIDENCE_CUES):
        confidence = said_confidence
    if confidence:
        values["confidence"] = confidence
    t = unicodedata.normalize("NFKC", note)
    metres = [n for _, n in _amounts(_METRES, t)]
    degrees = [n for _, n in _amounts(_DEGREES, t)]
    have = numbers_in(note)
    compass = compass_in(note) if not degrees else None
    for key, name, high, units, unit in (("長さ", "length", 2000.0, metres, "m"), ("幅", "width", 500.0, metres, "m"),
                                         ("向き", "heading", 360.0, degrees, "度")):
        if key not in got or name == "heading" and compass:
            continue
        n = _number(bare(got[key]))
        if n is None:
            dropped.append(f"{key}「{bare(got[key])}」は数字でない")
        elif not any(_same(n, h) for h in have):
            dropped.append(f"{key} {n} は備考にない数字")
        elif not any(_same(n, u) for u in units):
            dropped.append(f"{key} {n} は備考で{unit}の数字でない")
        elif not 0 <= float(n) <= high:
            dropped.append(f"{key} {n} は範囲外")
        else:
            values[name] = float(n)
    for at, n in _amounts(_METRES, t):
        # 「全長120m」「幅 20 m」: the 備考 says which it is.
        before = t[max(0, at - 6):at]
        name, high = ("width", 500.0) if "幅" in before else ("length", 2000.0) if "長" in before else (None, 0.0)
        if name and name not in values and 0 <= float(n) <= high:
            values[name] = float(n)
    if "heading" not in values and len(set(degrees)) == 1 and 0 <= float(degrees[0]) <= 360:
        values["heading"] = float(degrees[0])
    if "heading" not in values and compass:
        # 「針路は南東」「北上中」: the 備考's words as degrees.
        values["heading"] = compass[0]
    if "モノ" in got:
        name = unicodedata.normalize("NFKC", bare(got["モノ"]))
        classes = {_fold(_leaf(k)) for k in kinds} | {_fold(_root(k)) for k in kinds} | {_fold(w) for w in GENERIC_NAMES}
        if not name or "〇〇" in name or "○○" in name or _fold(name) in classes or _CLASS_WORD.match(name):
            dropped.append(f"モノ「{name}」は種類の名前（固有の名前でない）")
        else:
            for c in sorted(classes, key=len, reverse=True):
                if c and _fold(name).startswith(c) and len(name) - len(c) >= 2:
                    name = name[len(c):].lstrip(" 　の・")
                    break
            if _phrase(name, words):
                dropped.append(f"モノ「{name}」は名前でなく文")
            elif _fold(name) in folded:
                values["object"] = name
            else:
                dropped.append(f"備考にない名前「{name}」")
    order = ("kind", "activity", "confidence", "length", "width", "heading", "object")
    return FieldsDraft({k: values[k] for k in order if k in values}, tuple(dropped))


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
        ("去年", date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    ]


#: Other words for the periods of date_hints.
_PERIOD_WORDS = {"本日": "今日", "おととい": "一昨日", "昨年": "去年", "前年": "去年", "今週中": "今週"}
_RECENT = re.compile(r"(?:直近|過去|最近|ここ)\s*(\d+)\s*(日|週|か月|ヶ月|カ月|ケ月|ヵ月)")
#: Words that a request names a period with (one not among date_hints: the model's reading is kept).
_DATE_CUE = re.compile(r"\d|[月日週年]|まで|から|以降|以前|以来")


#: 「11月10日」「11/10〜15日」「2026年11月10日から11月15日」 (a day, or from one to another).
_DAYS = re.compile(r"(?:(?P<y>\d{4})[年/-])?(?P<m>\d{1,2})[月/](?P<d>\d{1,2})日?"
                   r"(?:\s*(?:から|〜|~|-|ー)\s*(?:(?:(?P<y2>\d{4})[年/-])?(?P<m2>\d{1,2})[月/])?(?P<d2>\d{1,2})日?)?")
#: 「10月」「2026年10月」 (the whole month).
_MONTH = re.compile(r"(?:(?P<y>\d{4})年)?(?<![\d/])(?P<m>\d{1,2})月(?![\d日])")


def _day_of(year: Optional[str], month: str, day: str, today: date) -> Optional[date]:
    """A day of the request (no year: this year's, or last year's when it would be more than a week ahead)."""
    try:
        d = date(int(year) if year else today.year, int(month), int(day))
    except ValueError:
        return None
    if not year and d > today + timedelta(days=7):
        d = d.replace(year=d.year - 1) if not (d.month == 2 and d.day == 29) else d
    return d


def period_in(words: str, today: date) -> Optional[tuple[date, date]]:
    """
    The period a request names (「先週」, 「直近30日」, 「昨日から今日」,
    「11月10日から15日」, 「10月」: from the first to the last); None when it
    names none.
    """
    t = unicodedata.normalize("NFKC", words)
    spans: list[tuple[date, date]] = []
    for m in _RECENT.finditer(t):
        n = int(m[1])
        days = n if m[2] == "日" else 7 * n if m[2] == "週" else 30 * n
        if 0 < days <= 3660:
            spans.append((today - timedelta(days=days - 1), today))
    rest = _RECENT.sub(" ", t)
    for m in _DAYS.finditer(rest):
        first = _day_of(m["y"], m["m"], m["d"], today)
        last = _day_of(m["y2"] or m["y"], m["m2"] or m["m"], m["d2"], today) if m["d2"] else first
        if first and last:
            spans.append((min(first, last), max(first, last)))
    rest = _DAYS.sub(" ", rest)
    for m in _MONTH.finditer(rest):
        first = _day_of(m["y"], m["m"], "1", today)
        if first:
            spans.append((first, (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)))
    rest = _MONTH.sub(" ", rest)
    named = {name: (a, b) for name, a, b in date_hints(today) if not name.startswith("直近")}
    for word in sorted(list(named) + list(_PERIOD_WORDS), key=len, reverse=True):
        if word in rest:
            spans.append(named[_PERIOD_WORDS.get(word, word)])
            rest = rest.replace(word, " ")
    if not spans:
        return None
    return min(a for a, _ in spans), max(b for _, b in spans)


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


_UNSEEN_CUES = ("雲", "見えな", "不可", "判読でき", "視認でき", "見られな")
_ABSENT_CUES = ("消失", "いなくな", "居なくな", "不在", "消え", "なくな")
_PRESENT_CUES = ("ありのみ", "有無はあり", "有無:あり", "あった", "いた", "いる", "存在", "在泊")


def presence_in(words: str) -> Optional[str]:
    """The 有無 a request names: 雲・見えない → "unseen", 消失・いなくなった → "absent", いた・ある → "present"."""
    t = _fold(words)
    for presence, cues in (("unseen", _UNSEEN_CUES), ("absent", _ABSENT_CUES), ("present", _PRESENT_CUES)):
        if any(c in t for c in cues):
            return presence
    return None


def _shares(a: str, b: str, n: int = 2) -> bool:
    """Whether `a` and `b` have `n` characters in a row in common."""
    return any(a[i:i + n] in b for i in range(len(a) - n + 1))


def _shares_kanji(a: str, b: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" and c in b for c in a)


def _asked(word: str, folded: str, today: Optional[date], places: Sequence[str], others: Sequence[str]) -> bool:
    """
    Whether the person wrote `word` for this condition: in the words, and
    not a word of another (the model puts 「消失」 in the 大分類 line too).
    """
    w = _fold(word)
    return bool(w) and w in folded and presence_in(word) is None and not (today and period_in(word, today)) and not any(
        _fold(x) == w for x in [*places, *others])


def search_draft(raw: str, places: Sequence[str], roots: Sequence[str], confidences: Sequence[str], words: str = "",
                 today: Optional[date] = None, kinds: Sequence[str] = ()) -> SearchDraft:
    """
    The conditions of a request: what `words` names itself (a period of
    date_hints, a place among `places`, a 大分類 or a class of `kinds`, a 確度,
    an 有無) wins; else the model's value is kept where the words give it a
    footing (a date word, a place sharing two characters, a 大分類 sharing a
    kanji). The model's 確度 and 有無 the words do not name are left out (a
    small model writes 「推定」「あり」 for every request).
    """
    got = answer_lines(raw, SEARCH_KEYS)
    dropped: list[str] = []
    t = _fold(words)
    start = end = None
    period = period_in(words, today) if words and today else None
    if period:
        start, end = period
    elif "期間" in got and (not words or _DATE_CUE.search(unicodedata.normalize("NFKC", words))):
        start, end = _dates_of(got["期間"])
        if start is None and end is None:
            dropped.append(f"日付として読めない期間「{bare(got['期間'])}」")
        elif start and end and start > end:
            start, end = end, start
    place = None
    said = sorted((p for p in places if _fold(p) and _fold(p) in t), key=lambda p: len(_fold(p)), reverse=True)
    if said:
        place = said[0]
    elif "地名" in got:
        value = bare(got["地名"])
        match = next((p for p in places if _fold(p) == _fold(value)), None)
        if match is None:
            dropped.append(f"地名の候補にない「{value}」")
        elif not words or _shares(_fold(match), t):
            place = match
    chosen = [r for r in roots if _named(r, t)] + [_root(k) for k in kinds_in(words, kinds) if _root(k) in roots]
    if not chosen and "大分類" in got:
        for word in re.split(r"[、,・/\s]+", bare(got["大分類"])):
            match = next((r for r in roots if _fold(r) == _fold(word)), None)
            if match is None and word.strip() and (not words or _asked(word, t, today, places, confidences)):
                dropped.append(f"大分類にない「{word}」")
            elif match and (not words or _shares_kanji(match, t)):
                chosen.append(match)
    named = [c for c in confidences if (len(_fold(c)) >= 2 and _fold(c) in t) or any(
        f"確度{a}{_fold(c)}" in t for a in ("", "は", "が", ":"))]
    confidence = min(named, key=lambda c: t.find(_fold(c))) if named else None
    if confidence is None and "確度" in got:
        word = re.sub(r"\s*以上$", "", bare(got["確度"]))
        match = next((c for c in confidences if _fold(c) == _fold(word)), None)
        if match is None and (not words or _asked(word, t, today, places, roots)):
            dropped.append(f"確度にない「{bare(got['確度'])}」")
        elif match and not words:
            confidence = match
    presence = presence_in(words) if words else None
    if presence is None and "有無" in got and not words:
        word = _fold(bare(got["有無"]))
        presence = ("present" if word.startswith(("あり", "在")) else "absent" if word.startswith(("消失", "不在", "いない"))
                    else "unseen" if word.startswith(("不可", "見えない")) else None)
        if presence is None:
            dropped.append(f"有無にない「{bare(got['有無'])}」")
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
    english = foreign_letters(source, text)
    return Draft(text, foreign_numbers(source, text), notes=(f"英語が混じる: {'・'.join(english)}",) if english else ())


# ---- 英訳 ----------------------------------------------------------------------------------------

#: English for words of image notes that a small model gets wrong (「航跡」 left as it is, 「岸壁」 as 「rocky shore」):
#: those in the 備考 are given with it.
TRANSLATE_TERMS: dict[str, str] = {
    "航跡": "wake", "岸壁": "quay", "停泊": "moored", "駐機": "parked", "掩体": "aircraft shelter", "エプロン": "apron",
    "格納庫": "hangar", "滑走路": "runway", "判読不可": "not interpretable", "入港": "entering port", "出港": "leaving port",
    "荷役": "cargo handling", "補給艦": "replenishment ship", "護衛艦": "destroyer", "潜水艦": "submarine",
    "哨戒機": "patrol aircraft", "戦闘機": "fighter", "輸送機": "transport aircraft", "北上": "heading north",
    "南下": "heading south", "東進": "heading east", "西進": "heading west", "港外": "outside the port",
    "港内": "inside the port", "集積": "stockpiled", "消失": "no longer present",
}
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")


def translate_terms(note: str) -> list[tuple[str, str]]:
    """TRANSLATE_TERMS' words in `note` (a longer one first; one inside it not repeated)."""
    out: list[tuple[str, str]] = []
    rest = note
    for word in sorted(TRANSLATE_TERMS, key=len, reverse=True):
        if word in rest:
            out.append((word, TRANSLATE_TERMS[word]))
            rest = rest.replace(word, " ")
    return [(w, e) for w, e in sorted(out, key=lambda x: note.find(x[0]))]


def translate_messages(task: TextTask, note: str) -> list[Message]:
    content = f"Note: {note.strip()[:400]}"
    terms = translate_terms(note.strip()[:400])
    if terms:
        content += "\nTerms: " + "; ".join(f"{w} = {e}" for w, e in terms)
    return [{"role": "system", "content": task.prompt}, {"role": "user", "content": content}]


def translated(raw: str, note: str) -> Draft:
    """
    The model's English (one line, without a label or quotes), the numbers
    in it the 備考 does not have, and the Japanese it left.
    """
    text = clean_note(raw)
    for _ in range(2):
        text = re.sub(r"^(?:English|Translation|Note|Terms)\s*:\s*", "", text, flags=re.IGNORECASE).strip()
        text = _unquoted(text)
    # 「The satellite image analysis notes state that there are …」 (sarashina2.2 3B): what the note says, only.
    said = re.match(r"^(?:the\s+)?[\w\s-]{0,40}?notes?\s+(?:states?|says?|reads?|indicates?)\s+that\s+(.+)$", text, re.IGNORECASE)
    if said:
        text = said[1][0].upper() + said[1][1:]
    left = list(dict.fromkeys(_JAPANESE.findall(text)))
    notes = (f"日本語が残る「{'・'.join(left[:3])}」",) if left else ()
    return Draft(text, foreign_numbers(note, text), notes=notes)


def today_of(now_ms: float) -> date:
    """The local date of a moment (ms)."""
    return datetime.fromtimestamp(now_ms / 1000).date()
