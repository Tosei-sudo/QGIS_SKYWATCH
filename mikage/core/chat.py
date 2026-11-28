"""
ミカゲと話す (tsudo 2026-11-28: 「文で操作の機能は全廃しよう。名前は任せるけど、
チャットで質問も操作もできるように」; design doc 「チャットで質問と操作（文で操作の
廃止） 設計方針案」, card 「この案で進める」).

One chat for the questions on the data (core/data_chat.py) and the
operations that 文で操作 had (core/text_command.py: the list, the words that
name them and the values read from a sentence). Code routes each sentence
(route()), in this order, and the model is asked only at the end:

1. FOLLOW: it points at what the answer before listed (「いちばん新しいのを
   開いて」, 「2枚目を地図で」): those images or records opened, no model;
2. IMAGES: it looks for images of a place (a named area, a point, the map's
   extent: 「呉で今週のSAR画像ある？」): the catalog searched (read only) and the
   count and the newest answered, no model;
3. OPERATION: it is no question and an operation's words name it (「Aシステム
   の明日のオーダーファイルを出力して」): that operation's card, no model;
4. MODEL: anything else. The model writes an SQL (the question answered
   from the tables) or 「操作: 名前」 (that operation's card).

A question is looked at before the operations' words: the field of 文で操作
sent 10 of the 30 questions of the study to an operation by their words
(「今日の撮像予定は何件？」 opened 撮像予定).

This module is pure Python.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

from .data_chat import TASK, is_question
from .text_command import Operation, by_words

__all__ = ["TASK", "TITLE", "OPEN_LIMIT", "FOLLOW", "IMAGES", "OPERATION", "MODEL", "Shown", "Route", "route", "follow_up",
           "shown_of", "image_search", "asks"]

TITLE = "ミカゲと話す"
#: The most images or records one follow-up opens.
OPEN_LIMIT = 10

FOLLOW, IMAGES, OPERATION, MODEL = "follow", "images", "operation", "model"


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


# ---- 1. what the answer before listed -------------------------------------------------------------------

@dataclass(frozen=True)
class Shown:
    """What an answer listed that the next sentence may point at: images or records, in the order shown."""
    #: "image" (画像ID) or "record" (記録ID).
    kind: str
    ids: tuple[str, ...]
    #: Their times as text (ISO, "" when not known), for 「いちばん新しい」.
    times: tuple[str, ...] = ()


_OPEN = re.compile(r"開いて|開く|開け|開きたい|表示して|表示する|見せて|見たい|地図で|地図に|出して|ズーム|移動して")
_EVERY = re.compile(r"全部|すべて|全て|みんな|全件|全枚|両方")
_NEWEST = re.compile(r"(?:いちばん|一番|最も)新し|最新|新しいほう|新しい方")
_OLDEST = re.compile(r"(?:いちばん|一番|最も)古|最古|古いほう|古い方")
_NTH = re.compile(r"([0-9]+|[一二三四五六七八九十])\s*(?:枚目|件目|番目|つ目|行目)")
_THAT = re.compile(r"それ|これ(?!から|まで)|その|この(?!範囲|辺|あたり|へん)|あれ|上の|さっきの|最初の")
_KANJI_NUMBERS = "一二三四五六七八九十"


def follow_up(text: str, shown: Optional[Shown]) -> Optional[list[int]]:
    """
    The places in `shown` that `text` points at to open (「いちばん新しいのを開いて」, 「2枚目を地図で」, 「全部開いて」,
    an id it lists); None when it is no follow-up (no word of opening, nothing it points at, nothing shown).
    """
    if shown is None or not shown.ids:
        return None
    t = _nfkc(text)
    if not _OPEN.search(t):
        return None
    named = [i for i, x in enumerate(shown.ids) if x and _nfkc(x) in t]
    if named:
        return named[:OPEN_LIMIT]
    if _EVERY.search(t):
        return list(range(min(len(shown.ids), OPEN_LIMIT)))
    m = _NTH.search(t)
    if m:
        n = int(m[1]) if m[1].isdigit() else _KANJI_NUMBERS.index(m[1]) + 1
        return [n - 1] if 0 < n <= len(shown.ids) else None
    times = [(x, i) for i, x in enumerate(shown.times) if x]
    if _NEWEST.search(t):
        return [max(times)[1]] if times else [0]
    if _OLDEST.search(t):
        return [min(times)[1]] if times else [len(shown.ids) - 1]
    if "最後の" in t:
        return [len(shown.ids) - 1]
    if _THAT.search(t):
        return [0]
    return None


#: The labels of the answer's columns (core/data_chat NOTES) that list images and records, and their times.
IMAGE_LABEL, RECORD_LABEL, TIME_LABELS = "画像ID", "記録ID", ("撮像日時", "登録日時")
#: The most rows of an answer kept for a follow-up.
SHOWN_ROWS = 200


def shown_of(columns: Sequence[str], rows: Sequence[Sequence], labels: dict) -> Optional[Shown]:
    """
    What an answer's table lists for a follow-up: its image ids (a column labelled 画像ID), else its records (記録ID),
    with their times (撮像日時, else 登録日時) in the rows' order; None when it lists neither.
    """
    named = [labels.get(str(c).upper(), "") for c in columns]
    for kind, label in (("image", IMAGE_LABEL), ("record", RECORD_LABEL)):
        if label not in named:
            continue
        i = named.index(label)
        t = next((named.index(x) for x in TIME_LABELS if x in named), None)
        ids, times = [], []
        for row in rows[:SHOWN_ROWS]:
            v = row[i] if i < len(row) else None
            if v is None or str(v).strip() == "":
                continue
            ids.append(str(v).strip())
            times.append(str(row[t]) if t is not None and t < len(row) and row[t] is not None else "")
        return Shown(kind, tuple(ids), tuple(times)) if ids else None
    return None


# ---- 2. images of a place ----------------------------------------------------------------------------------

_IMAGE = re.compile(r"画像|写真|シーン")
_LOOK = re.compile(r"ある|あり|探|見せ|見たい|見て|出して|一覧|何枚|検索|撮れ|撮った|撮影")
#: A question on images that is no search (their records, counts by something): the tables answer it.
_NOT_SEARCH = re.compile(r"記録|観測|検出|通報|依頼|分担|担当|レビュー|評価|ごと|別に|割合|比べ|比較|平均|合計|推移")
_MAP_AREA = re.compile(r"地図の(?:表示)?範囲|表示範囲|この範囲|この辺|このあたり|この辺り|ここ")


def image_search(text: str, known_place: bool, point: bool) -> bool:
    """
    Whether `text` looks for images of a place (the catalog searched): images and a word of looking, a place that is
    known (`known_place`: one of the named areas), a point (`point`) or the map's extent, and nothing that makes it a
    question for the tables (records, counts by something).
    """
    t = _nfkc(text)
    if not (_IMAGE.search(t) and _LOOK.search(t)) or _NOT_SEARCH.search(t):
        return False
    return known_place or point or bool(_MAP_AREA.search(t))


# ---- the route -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    kind: str
    #: FOLLOW: the places in the shown list opened.
    opened: tuple[int, ...] = ()
    #: OPERATION: the operation its words name.
    operation: Optional[Operation] = None


_MORE_QUESTION = re.compile(r"順に|順で|多いもの|少ないもの|[0-9]+件(?!目)|上位|トップ")


def asks(text: str) -> bool:
    """Whether `text` asks about the data (data_chat.is_question, and lists in an order: 「新しい順に10件」)."""
    return is_question(text) or bool(_MORE_QUESTION.search(_nfkc(text)))


def route(text: str, shown: Optional[Shown], ops: Sequence[Operation], more_words: Optional[dict] = None,
          screens: Sequence[str] = (), known_place: bool = False, point: bool = False) -> Route:
    """Where `text` goes (the module's order); `known_place` / `point`: what the catalog's reading found in it."""
    named = by_words(text, ops, more_words, screens)
    # 「その日の計画ファイルを出して」: an operation, not the list before (only 画像検索・移動 may point at it).
    opened = follow_up(text, shown) if named is None or named.key in ("catalog", "jump") else None
    if opened is not None:
        return Route(FOLLOW, tuple(opened))
    if any(o.key == "catalog" for o in ops) and image_search(text, known_place, point):
        return Route(IMAGES)
    if not asks(text) and named is not None:
        return Route(OPERATION, operation=named)
    return Route(MODEL)
