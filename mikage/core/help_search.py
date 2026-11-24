"""
The search of 「使い方（ヘルプ）」 (ui/help_dialog.py), on the page's plain text.

Words are split at spaces (full-width too) and all of them must be in a
section (from one h2/h3 heading to the next) for its hits to count, like the
catalog's フリーワード. Width, case and kana are folded: 「ＭＩＫＡＧＥ」 finds
「MIKAGE」, 「もの」 finds 「モノ」, a half-width 「ｶﾞｲﾄﾞ」 finds 「ガイド」.

Positions are those of the text given (the help window gives its document's
plain text, whose positions are the document's), so each hit can be marked there.
"""
from __future__ import annotations

import bisect
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

#: The part of the page before its first heading (the title and the table of what is where).
TOP = ""


def words_of(text: str) -> list[str]:
    """The words of a search: split at spaces (full-width too), each once, in the order typed."""
    return list(dict.fromkeys((text or "").split()))


def folded(text: str) -> tuple[str, list[int]]:
    """
    `text` with width, case and kana folded (NFKC, lower, katakana as
    hiragana), and for each of its characters the index in `text` of the
    character it comes from. A half-width voiced mark (ｶﾞ) joins the one before it.
    """
    out: list[str] = []
    at: list[int] = []
    for i, c in enumerate(text):
        n = unicodedata.normalize("NFKC", c).lower()
        if n and out and all(unicodedata.combining(ch) for ch in n):
            out[-1] = unicodedata.normalize("NFC", out[-1] + n)
            continue
        for ch in n:
            out.append(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch)
            at.append(i)
    return "".join(out), at


def fold(text: str) -> str:
    return folded(text)[0]


@dataclass
class Found:
    """What a search found."""
    words: list[str]
    #: (start, end) in the text, in order, none inside another.
    hits: list[tuple[int, int]] = field(default_factory=list)
    #: The number of hits in each section (by heading id; TOP before the first), those with every word only.
    counts: dict[str, int] = field(default_factory=dict)


class Index:
    """The page's text, folded once, and where its sections start."""

    def __init__(self, text: str, starts: list[tuple[int, str]]):
        """`starts`: (position, id) of each heading, in order."""
        self.text = text
        self.folded, self.at = folded(text)
        self.positions = [p for p, _id in starts]
        self.ids = [i for _p, i in starts]

    def section_at(self, position: int) -> str:
        """The id of the section holding that position (TOP before the first heading)."""
        i = bisect.bisect_right(self.positions, position) - 1
        return self.ids[i] if i >= 0 else TOP

    def search(self, text: str) -> Found:
        words = words_of(text)
        found = Found(words)
        if not words:
            return found
        by_section: dict[str, set[str]] = {}
        hits = []
        for word in words:
            needle = fold(word)
            if not needle:
                continue
            i = self.folded.find(needle)
            while i >= 0:
                start, end = self.at[i], self.at[i + len(needle) - 1] + 1
                section = self.section_at(start)
                by_section.setdefault(section, set()).add(word)
                hits.append((start, end, section))
                i = self.folded.find(needle, i + len(needle))
        kept = {s for s, have in by_section.items() if len(have) == len(words)}
        last_end = -1
        for start, end, section in sorted(hits):
            if section not in kept:
                continue
            if start < last_end:
                # Inside the one before (「依頼」 within 「依頼文」): one hit, as long as both.
                if end > last_end:
                    found.hits[-1] = (found.hits[-1][0], end)
                    last_end = end
                continue
            found.hits.append((start, end))
            found.counts[section] = found.counts.get(section, 0) + 1
            last_end = end
        return found

    def first_in(self, found: Found, start: int, end: Optional[int] = None) -> Optional[int]:
        """The index of the first hit from `start` (before `end`), if any."""
        i = bisect.bisect_left(found.hits, (start, -1))
        if i < len(found.hits) and (end is None or found.hits[i][0] < end):
            return i
        return None
