"""
備考の単語集 (tsudo, 2026-11-17: 「観測結果の備考とか入力しているときに、
単語集さえあれば、単語のサジェスチョンって出せる？使用する語彙を(できるだけ)
統制したい」).

The settings file's `glossary` names an Esri table (TBL_GLOSSARY) of the
words the 備考 should use, a row each: the word, its other spellings
(表記ゆれ: to be written as the word), its readings (hiragana, so that a word
is found by typing its reading), the 大分類 it is for (blank: every one), a
description and an order. The words are kept with the data, not in the
settings file (which only names the table).

Japanese is written without spaces, so a suggestion completes the end of the
text before the cursor, not the whole field: the longest end that starts a
word (or its reading, or another spelling of it) is the part being typed, and
the word chosen takes its place. Other spellings found in the text are
pointed out with the word to write instead; saving is never stopped.

The classification's known individuals and their other names (別名) are
offered too (`member_terms`, tsudo 2026-11-27), for their 大分類; they are
only offered, never pointed out as another spelling.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

from ..esri import EsriClient, EsriError
from ..util import js_string

#: The table's attributes by role (`glossary.fields` names others).
DEFAULT_GLOSSARY_FIELDS = {
    "term": "TERM", "variants": "VARIANTS", "reading": "READING", "kinds": "ROOT_KIND",
    "description": "DESCRIPTION", "order": "SORT_ORDER",
}
ROLE_LABELS = {"term": "語", "variants": "表記ゆれ", "reading": "よみ", "kinds": "大分類", "description": "説明", "order": "並び順"}
#: Roles without which the table is not used.
NEEDED = ("term",)
#: More rows than this is an error (never cut silently).
LIMIT = 50_000
#: How many suggestions are offered at once.
SUGGESTIONS = 12
#: The longest end of the text tried (no word is longer).
LONGEST = 40
#: Between the words of an attribute that holds several (「入航, 入湊」).
_LIST = re.compile(r"[,、，;；\r\n]+")
#: What ends the part being typed: spaces, punctuation and brackets (words run on without spaces otherwise).
SEPARATORS = frozenset(" \t\r\n　、。，．,.・;；:：!?！？()（）「」『』[]［］【】〈〉《》<>＜＞/／\\|｜\"'“”‘’=＝+＋~〜")


def _text(v: Any) -> str:
    return "" if v is None else js_string(v).strip()


def listed(value: Any) -> tuple[str, ...]:
    """The words of an attribute holding several (commas, 、 or semicolons between them), blanks and repeats left out."""
    if value is None or isinstance(value, (dict, bool)):
        return ()
    items = value if isinstance(value, (list, tuple)) else _LIST.split(_text(value))
    return tuple(dict.fromkeys(t for t in (str(i).strip() for i in items) if t))


def fold(text: str) -> str:
    """How words are compared: width and case folded (NFKC, lower), katakana as hiragana."""
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub("[ァ-ヶ]", lambda m: chr(ord(m[0]) - 0x60), t)


def _width_case(text: str) -> tuple[str, list[int]]:
    """
    `text` with width and case folded (NFKC, lower; kana kept as written), and
    for each character of it the index of the character of `text` it comes
    from (a half-width ｶﾞ is one character, and points at the ｶ).
    """
    out: list[str] = []
    at: list[int] = []
    for i, c in enumerate(text):
        n = unicodedata.normalize("NFKC", c).lower()
        if n and out and all(unicodedata.combining(ch) for ch in n):
            # A voiced mark of half-width katakana: one character with the one before it.
            out[-1] = unicodedata.normalize("NFC", out[-1] + n)
            continue
        for ch in n:
            out.append(ch)
            at.append(i)
    return "".join(out), at


def _hiragana_only(text: str) -> bool:
    return bool(text) and all("ぁ" <= c <= "ゖ" or c == "ー" for c in text)


def _latin(c: str) -> bool:
    return c.isascii() and c.isalnum()


@dataclass(frozen=True)
class Term:
    """One word of the glossary."""
    text: str
    #: Other spellings, to be written as `text`.
    variants: tuple[str, ...] = ()
    #: Readings (hiragana or katakana).
    readings: tuple[str, ...] = ()
    #: The 大分類 it is for; () for every one.
    kinds: tuple[str, ...] = ()
    description: str = ""
    order: Optional[float] = None
    #: Whether it is written as the glossary says (another width or case pointed out); False: only offered.
    fixes: bool = True

    def for_kind(self, root: str) -> bool:
        """Whether the word is offered while recording a thing of the 大分類 `root` ("" when none is chosen)."""
        return not self.kinds or not root or root in self.kinds


@dataclass(frozen=True)
class Suggestion:
    """A word offered for the part being typed."""
    term: Term
    #: Where the part being typed starts in the text before the cursor (the word takes its place).
    start: int
    #: The part being typed.
    typed: str
    #: What it matched: "" the word itself, "reading" a reading, else the other spelling typed.
    via: str = ""

    @property
    def label(self) -> str:
        """The line in the list: the word, the other spelling typed, and the description."""
        text = self.term.text
        if self.via and self.via != "reading":
            text += f"（{self.via} は単語集では {self.term.text}）"
        if self.term.description:
            text += f"　{self.term.description}"
        return text


@dataclass(frozen=True)
class Variant:
    """Another spelling of a word found in the text (characters start…end of the text)."""
    start: int
    end: int
    found: str
    term: str


@dataclass(frozen=True)
class _Key:
    folded: str
    term: int
    #: "" the word, "reading", else the other spelling.
    via: str


class Glossary:
    """The words of the glossary, looked up by how they start."""

    def __init__(self, terms: Iterable[Term] = ()):
        merged: dict[str, Term] = {}
        for t in terms:
            text = t.text.strip()
            if not text:
                continue
            old = merged.get(text)
            if old is None:
                merged[text] = Term(text, tuple(v for v in t.variants if v != text), t.readings, t.kinds, t.description, t.order, t.fixes)
                continue
            # The same word on two rows (for two 大分類, say): one word, every spelling, reading and 大分類 of both.
            merged[text] = Term(
                text, tuple(dict.fromkeys(v for v in (*old.variants, *t.variants) if v != text)),
                tuple(dict.fromkeys((*old.readings, *t.readings))),
                () if not old.kinds or not t.kinds else tuple(dict.fromkeys((*old.kinds, *t.kinds))),
                old.description or t.description, old.order if old.order is not None else t.order, old.fixes or t.fixes)
        self.terms: tuple[Term, ...] = tuple(merged.values())
        keys: list[_Key] = []
        for i, t in enumerate(self.terms):
            keys.append(_Key(fold(t.text), i, ""))
            keys += [_Key(fold(r), i, "reading") for r in t.readings if fold(r)]
            keys += [_Key(fold(v), i, v) for v in t.variants if fold(v)]
        self._keys = sorted(keys, key=lambda k: (k.folded, k.term, k.via))
        self._folded = [k.folded for k in self._keys]
        self._words = {t.text for t in self.terms}

    def __bool__(self) -> bool:
        return bool(self.terms)

    def __len__(self) -> int:
        return len(self.terms)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Glossary) and self.terms == other.terms

    def __hash__(self) -> int:
        return hash(self.terms)

    def with_terms(self, extra: Sequence[Term]) -> "Glossary":
        """The glossary with more words after its own (a word it has keeps its own description)."""
        return Glossary([*self.terms, *extra]) if extra else self

    def words(self, root: str = "") -> list[str]:
        """The words offered for the 大分類 `root`, in the glossary's order."""
        return [t.text for t in self._ordered(t for t in self.terms if t.for_kind(root))]

    @staticmethod
    def _ordered(terms: Iterable[Term]) -> list[Term]:
        return sorted(terms, key=lambda t: (t.order is None, t.order or 0))

    def _starting(self, q: str) -> list[_Key]:
        i = bisect.bisect_left(self._folded, q)
        out = []
        while i < len(self._keys) and self._keys[i].folded.startswith(q):
            out.append(self._keys[i])
            i += 1
        return out

    def suggest(self, before: str, root: str = "", limit: int = SUGGESTIONS) -> list[Suggestion]:
        """
        The words for the part being typed at the end of `before` (the text
        before the cursor), while recording a thing of the 大分類 `root`: the
        longest end that starts a word, its reading or another spelling of it.
        Nothing for one hiragana (a particle is not the start of a word), nor
        for a word typed out in full (unless a longer word starts with it).
        """
        if not self.terms or not before:
            return []
        start = len(before)
        while start > 0 and before[start - 1] not in SEPARATORS and len(before) - start < LONGEST:
            start -= 1
        part = before[start:]
        for n in range(len(part), 0, -1):
            typed = part[-n:]
            if _hiragana_only(typed) and n < 2:
                continue
            if _latin(typed[0]) and len(part) > n and _latin(part[-n - 1]):
                continue  # Inside a word of letters: 「AIS」 is not typed in 「CHAIS」.
            q = fold(typed)
            if not q:
                continue
            found: dict[int, _Key] = {}
            for k in self._starting(q):
                term = self.terms[k.term]
                if not term.for_kind(root):
                    continue
                if k.via == "" and k.folded == q and term.text == typed:
                    continue  # Written out already.
                best = found.get(k.term)
                # A word matched by itself first, then by a reading, then by another spelling (the one typed in full first).
                if best is None or self._rank(k, q) < self._rank(best, q):
                    found[k.term] = k
            if not found:
                continue
            ranked = sorted(found.values(), key=lambda k: (
                0 if k.via not in ("", "reading") and k.folded == q else 1,
                self.terms[k.term].order is None, self.terms[k.term].order or 0,
                0 if self.terms[k.term].kinds else 1, len(self.terms[k.term].text), self.terms[k.term].text))
            return [Suggestion(self.terms[k.term], len(before) - n, typed, k.via) for k in ranked[:limit]]
        return []

    @staticmethod
    def _rank(k: _Key, q: str) -> tuple:
        return (0 if k.via == "" else 1 if k.via == "reading" else 2, 0 if k.folded == q else 1, len(k.folded))

    def variants_in(self, text: str, root: str = "") -> list[Variant]:
        """
        The other spellings of words found in `text` (width and case aside),
        and words written in another width or case (「ＡＩＳ」 for 「AIS」):
        each with the word to write instead, in the order they come. Not one
        inside a longer word of the glossary (「停船」 in 「停船中」 when that is a
        word), nor one that is itself a word for `root`, nor letters inside a
        word of letters.
        """
        if not self.terms or not text:
            return []
        folded, at = _width_case(text)

        def spans(word: str):
            w = unicodedata.normalize("NFKC", word).lower()
            i = folded.find(w) if w else -1
            while i >= 0:
                j = i + len(w)
                left = folded[i - 1] if i > 0 else ""
                right = folded[j] if j < len(folded) else ""
                if not ((_latin(w[0]) and left and _latin(left)) or (_latin(w[-1]) and right and _latin(right))):
                    yield i, j
                i = folded.find(w, i + 1)

        # Where the words themselves are (a spelling inside a longer word is that word's).
        words = [(i, j) for t in self.terms for i, j in spans(t.text)]
        found: list[tuple[int, int, str]] = []
        for t in self.terms:
            if not t.fixes:
                continue
            for v in t.variants:
                if v in self._words and any(x.text == v and x.for_kind(root) for x in self.terms):
                    continue
                found += [(i, j, t.text) for i, j in spans(v)]
            found += [(i, j, t.text) for i, j in spans(t.text)]
        out: list[Variant] = []
        taken: list[tuple[int, int]] = []
        for i, j, term in sorted(found, key=lambda f: (f[0], -(f[1] - f[0]))):
            if any(a <= i and j <= b and (b - a) > (j - i) for a, b in words):
                continue
            if any(i < b and a < j for a, b in taken):
                continue
            s, e = at[i], at[j - 1] + 1
            if e < len(text) and unicodedata.combining(unicodedata.normalize("NFKC", text[e])):
                e += 1  # The voiced mark after the last character.
            written = text[s:e]
            if written == term:
                continue
            taken.append((i, j))
            out.append(Variant(s, e, written, term))
        return out

    def fixed(self, text: str, root: str = "") -> str:
        """`text` with every other spelling found written as its word."""
        for v in reversed(self.variants_in(text, root)):
            text = text[:v.start] + v.term + text[v.end:]
        return text


NO_GLOSSARY = Glossary()


def member_terms(taxa: Sequence[Any]) -> tuple[Term, ...]:
    """The classification's known individuals and their other names as words offered (never pointed out), each for
    its 大分類: 「かが　いずも型の艦名」, 「DDH-184　かがの別名（いずも型）」."""
    from .taxonomy import name_label_of

    out: list[Term] = []
    for t in taxa:
        if not t.members:
            continue
        root, cls = (t.path[0],), t.path[-1]
        label = name_label_of(taxa, t.path) or "個体"
        for name in t.members:
            out.append(Term(name, kinds=root, description=f"{cls}の{label}", fixes=False))
            out += [Term(a, kinds=root, description=f"{name}の別名（{cls}）", fixes=False) for a in t.member(name).aliases() if a != name]
    return tuple(out)


def term_of(attributes: dict, fields: dict) -> Optional[Term]:
    """One row of the table as a word (None without one)."""
    def get(role: str) -> Any:
        name = fields.get(role)
        return attributes.get(name) if name else None

    text = _text(get("term"))
    if not text:
        return None
    order = get("order")
    try:
        number = float(order) if order is not None and _text(order) != "" else None
    except (TypeError, ValueError):
        number = None
    return Term(text, listed(get("variants")), listed(get("reading")), listed(get("kinds")), _text(get("description")), number)


def glossary_of_rows(rows: Sequence[dict], fields: dict) -> Glossary:
    return Glossary(t for t in (term_of(r.get("attributes") or {}, fields) for r in rows) if t is not None)


def glossary_source_of(value: Any, problems: list[str]):
    """`glossary`: the table of words (URL or `{ "url", "fields", "where" }`)."""
    from .alerts import esri_source_of

    return esri_source_of(value, "glossary", DEFAULT_GLOSSARY_FIELDS, problems)


def load_glossary(client: EsriClient, source, token: Optional[str] = None,
                  cancelled: Optional[Callable[[], bool]] = None) -> Glossary:
    """Every word of the table (only the attributes it has are asked for)."""
    info = client.layer(source.url, token, refresh=True)
    missing = [source.fields[r] for r in NEEDED if source.fields[r] not in info.field_types]
    if missing:
        raise EsriError(f"単語集の表に必要な項目がない（管理者に連絡: {'・'.join(missing)}）", code="E704")
    fields = {role: name for role, name in source.fields.items() if name in info.field_types}
    rows = client.query_all(source.url, source.where or "1=1", token, out_fields=",".join(dict.fromkeys(fields.values())),
                            geometry=False, limit=LIMIT, cancelled=cancelled)
    return glossary_of_rows(rows, fields)

