"""
衛星の種別 (`satelliteKinds`; tsudo 2026-11-26: 「衛星の一番大きな種別にIRを足したい / コンフィグカスタマイズ
可能にできないかな？ / 文で操作もそれに応じてほしい」): the kinds satellites are of, in the order the screens
list them (the top level of the satellite trees, the 種別 of a request), and what each one needs.

A satellite's kind comes from its catalog's kind attribute (`fields.kind`, SENSOR_TYPE): the kind whose name it
is, else the first whose `match` finds something in it (a satellite catalog's `sarPattern` stands for the
cloud-free kind's), else the first kind with no `match` (光学 by default; the first kind when every one has one).

Each kind says whether it images only in daylight (`daylight`: the imaging plan's 日照条件, 時間帯 「夜」 of a
request) and whether clouds hide the ground from it (`clouds`: the cloud forecast, a request's 実施しない雲量,
either-kind requests going to a satellite clouds do not stop). A kind clouds do not stop is a radar (SAR: the
interferometric pairs, its satellites' `sar`). Its `words` are what sentences call it (文で操作's 画像検索, a
request's text).

Without the key: 光学, SAR and IR (thermal infrared: by night too, not through clouds).

Pure Python. The kinds in use are set when the settings are read (set_kinds; qgis_io/context.py), so that what
reads a kind or lists them (requests, sentences, trees) needs no settings of its own.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional, Sequence

#: The 種別 of a request that takes any kind.
ANY_KIND = "任意"
#: The longest name (a request's KIND is TEXT(16) in its Esri table).
NAME_LIMIT = 16
#: What may follow a kind's word in a sentence or the card's 衛星・種別 (「SAR画像」「光学衛星」).
_SUFFIX = r"(?:衛星|画像|センサー?|データ|カメラ)?"


def _nfkc(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or ""))


def _ascii_word(ch: str) -> bool:
    return bool(re.match(r"[A-Za-z0-9]", ch))


def _word_re(word: str, loose: bool = False) -> str:
    """
    A word as a pattern: latin letters only where no other latin letter is next to them (IR, not AIRBUS);
    `loose`: a latin word of three letters or more anywhere (TerraSAR), as a request's text was read before.
    """
    w = _nfkc(word).strip()
    p = re.escape(w)
    if loose and len(w) >= 3:
        return p
    if w and _ascii_word(w[0]):
        p = r"(?<![A-Za-z])" + p
    if w and _ascii_word(w[-1]):
        p += r"(?![A-Za-z])"
    return p


@dataclass(frozen=True)
class SatelliteKind:
    """One kind of `satelliteKinds`."""
    name: str
    #: Found in a catalog's kind attribute: the kind (None: only its name, or as the kind of the others).
    match: Optional[re.Pattern] = None
    #: What sentences call it (its name counts too).
    words: tuple = ()
    #: Images only in daylight (the plan's 日照条件; no 「夜」 request).
    daylight: bool = True
    #: Clouds hide the ground from it (cloud forecasts, 実施しない雲量).
    clouds: bool = True

    @property
    def radar(self) -> bool:
        """Clouds do not stop it: SAR."""
        return not self.clouds

    def all_words(self) -> list[str]:
        """Its name and words (NFKC), the longer first, each once."""
        found = [w for w in dict.fromkeys(_nfkc(x).strip() for x in (self.name, *self.words)) if w]
        return sorted(found, key=len, reverse=True)

    def label(self) -> str:
        """For the model's list of operations: 「SAR/レーダー」 (a latin name with its first other word), 「光学」."""
        name = _nfkc(self.name)
        if name.isascii():
            other = next((w for w in self.words if not _nfkc(w).isascii()), None)
            if other:
                return f"{name}/{_nfkc(other)}"
        return name


def _pattern(words: Sequence[str], loose: bool = False) -> re.Pattern:
    return re.compile("|".join(_word_re(w, loose) for w in words), re.IGNORECASE)


_OPTICAL = SatelliteKind(
    "光学", None, ("光学", "可視", "可視光", "optical", "パンクロ", "マルチスペクトル", "カラー画像"), daylight=True, clouds=True)
_SAR = SatelliteKind(
    "SAR", re.compile(r"SAR|radar|レーダ", re.IGNORECASE), ("SAR", "レーダー", "レーダ", "合成開口", "合成開口レーダー", "radar"),
    daylight=False, clouds=False)
_IR = SatelliteKind(
    "IR", re.compile(r"赤外|サーマル|(?<![A-Za-z])(?:T|LW|MW)?IR(?![A-Za-z])|thermal|infrared", re.IGNORECASE),
    ("IR", "赤外", "赤外線", "熱赤外", "熱赤外線", "サーマル", "thermal", "infrared"), daylight=False, clouds=True)
#: The kinds without `satelliteKinds`, and what a kind of one of these names takes from it when it does not say.
BUILT_IN = (_OPTICAL, _SAR, _IR)


@dataclass(frozen=True)
class SatelliteKinds:
    """The kinds in their order."""
    items: tuple = BUILT_IN

    @property
    def names(self) -> tuple:
        return tuple(k.name for k in self.items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def named(self, name: Any) -> Optional[SatelliteKind]:
        """The kind of that name (NFKC, case not minded); None when none."""
        n = _nfkc(name).strip().casefold()
        return next((k for k in self.items if _nfkc(k.name).casefold() == n), None) if n else None

    @property
    def other(self) -> SatelliteKind:
        """The kind of a satellite whose kind attribute is none of them: the first with no `match`, else the first."""
        return next((k for k in self.items if k.match is None), self.items[0])

    @property
    def radar(self) -> Optional[SatelliteKind]:
        """The first kind clouds do not stop (SAR): a catalog's `sarPattern` stands for its `match`."""
        return next((k for k in self.items if k.radar), None)

    def of_text(self, text: Any, sar_pattern: Optional[re.Pattern] = None) -> SatelliteKind:
        """The kind of a catalog's kind attribute (「SAR」「光学」「熱赤外」, None)."""
        if not isinstance(text, str) or not text.strip():
            return self.other
        own = self.named(text)
        if own is not None:
            return own
        found = self.matched(text, sar_pattern)
        return found if found is not None else self.other

    def matched(self, text: Any, sar_pattern: Optional[re.Pattern] = None) -> Optional[SatelliteKind]:
        """The first kind whose `match` (the radar's: `sar_pattern` when given) finds something in a text; None when none."""
        t = _nfkc(text)
        radar = self.radar
        for k in self.items:
            pattern = sar_pattern if sar_pattern is not None and k is radar else k.match
            if pattern is not None and pattern.search(t):
                return k
        return None

    def of_flag(self, sar: bool) -> SatelliteKind:
        """The kind of a satellite only told SAR or not (a sensor table's yes/no, an old call): the radar's, else the others'."""
        return (self.radar or self.other) if sar else self.other

    def request_kinds(self) -> tuple:
        """A request's 種別: 任意, then the kinds."""
        return (ANY_KIND, *self.names)

    # ---- sentences ---------------------------------------------------------------------------------------

    def kinds_in(self, text: str, loose: bool = False) -> list[str]:
        """The kinds a sentence names (their names), in the order written (`loose`: _word_re)."""
        t = _nfkc(text)
        found = []
        for k in self.items:
            m = _pattern(k.all_words(), loose).search(t)
            if m:
                found.append((m.start(), k.name))
        return [n for _at, n in sorted(found)]

    def kind_word(self, word: str) -> Optional[str]:
        """The kind a word is on its own (「SAR」「光学衛星」「赤外画像」「合成開口レーダー」), else None."""
        w = re.sub(r"\s+", "", _nfkc(word))
        if not w:
            return None
        for k in self.items:
            alts = "|".join(re.escape(x) for x in k.all_words())
            if re.fullmatch(f"(?:{alts})+{_SUFFIX}", w, re.IGNORECASE):
                return k.name
        return None

    def stop_words(self) -> set:
        """The kinds' words as no place is called (「SAR」「SAR画像」「光学衛星」)."""
        out = set()
        for k in self.items:
            for w in k.all_words():
                out |= {w, f"{w}画像", f"{w}衛星"}
        return out

    def describe(self) -> str:
        """For the model's list: 「光学かSAR/レーダーかIR/赤外か」."""
        return "".join(f"{k.label()}か" for k in self.items)


DEFAULT_KINDS = SatelliteKinds()


def _bool(v: Any, key: str, at: str, problems: list[str], fallback: bool) -> bool:
    if v is None:
        return fallback
    if isinstance(v, bool):
        return v
    problems.append(f"{at} の {key} は true か false で指定")
    return fallback


def satellite_kinds_of(value: Any, problems: list[str]) -> SatelliteKinds:
    """The kinds of `satelliteKinds`; the built-in ones (the reason in `problems`) when none can be used."""
    if not isinstance(value, list) or not value:
        problems.append("satelliteKinds は種別（name・match・words・daylight・clouds）の配列で指定")
        return DEFAULT_KINDS
    out: list[SatelliteKind] = []
    for i, v in enumerate(value):
        at = f"satelliteKinds[{i}]"
        if isinstance(v, str):
            v = {"name": v}
        if not isinstance(v, dict):
            problems.append(f"{at} はオブジェクトで指定")
            continue
        name = _nfkc(v.get("name")).strip() if isinstance(v.get("name"), str) else ""
        if not name:
            problems.append(f"{at} に name（種別の名前）なし")
            continue
        if name == ANY_KIND or any(_nfkc(k.name).casefold() == name.casefold() for k in out):
            problems.append(f"{at} の name「{name}」は{'依頼の「任意」と同じ' if name == ANY_KIND else '重複'}")
            continue
        if len(name) > NAME_LIMIT:
            problems.append(f"{at} の name は {NAME_LIMIT} 文字まで")
            continue
        base = next((k for k in BUILT_IN if k.name.casefold() == name.casefold()), None)
        match = base.match if base is not None and "match" not in v else None
        given = v.get("match")
        if isinstance(given, str) and given.strip():
            try:
                match = re.compile(given, re.IGNORECASE)
            except re.error:
                problems.append(f"{at} の match は正規表現として読めない")
        elif given not in (None, ""):
            problems.append(f"{at} の match は正規表現（文字列）で指定")
        words = v.get("words")
        if words is None:
            listed = tuple(base.words) if base is not None else ()
        elif isinstance(words, list) and all(isinstance(w, str) for w in words):
            listed = tuple(w.strip() for w in words if w.strip())
        else:
            problems.append(f"{at} の words は文字列の配列で指定")
            listed = tuple(base.words) if base is not None else ()
        out.append(SatelliteKind(
            name=name, match=match, words=listed,
            daylight=_bool(v.get("daylight"), "daylight", at, problems, base.daylight if base is not None else True),
            clouds=_bool(v.get("clouds"), "clouds", at, problems, base.clouds if base is not None else True),
        ))
    if not out:
        return DEFAULT_KINDS
    return SatelliteKinds(tuple(out))


_current: SatelliteKinds = DEFAULT_KINDS


def set_kinds(value: Optional[SatelliteKinds]) -> None:
    """Sets the kinds in use (None: the built-in ones)."""
    global _current
    _current = value or DEFAULT_KINDS


def kinds() -> SatelliteKinds:
    """The kinds in use."""
    return _current


def kind_of_sat(sat: Any) -> Optional[SatelliteKind]:
    """A satellite's kind (by its kind_name); None when it is of none in use."""
    return _current.named(getattr(sat, "kind_name", None))


def request_kinds() -> tuple:
    """A request's 種別 choices in use: 任意 and the kinds."""
    return _current.request_kinds()
