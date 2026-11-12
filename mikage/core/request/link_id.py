"""
外部連携ID (LINK_ID): a short id of each request handed out on a day to a
system, kept in its history row and written in the system's file
(`linkId`), so that the system's answer file (結果ファイル) can name the
very row it answers (tsudo, 2026-11-12: 「一部のシステムが名前にUUIDの長さに
対応していないので、12文字程度の、完全な唯一性はいいので、識別できる番号を。
システムからのインポート時に、日付の蓋然性とそのIDから特定の依頼と照合できるように」).

- 12 characters by default (`linkIdLength`, 6 … 32 per system), of digits and
  upper-case letters without I, L, O, U (Crockford's base 32: nothing read
  two ways), or digits only / hexadecimal (`linkIdChars`).
- Worked out from the request, the target day, the system and the time of
  the day (1 日の回数; and the shot, for an area shared out from its second),
  not drawn at random: the same request handed to the
  same system for the same day keeps its id when the day is written again
  (「置き換えて書き出し」), so an answer to either file finds its row.
- Not unique for ever (12 characters of base 32 are 60 bits, so two rows of
  a system sharing one are very unlikely, but a short or digits-only id can
  repeat over months): two of one day's file are told apart (the later one by
  key order is worked out again), and a result file's id is looked for among
  the system's rows of the day read and the days just before it, the row of
  the nearest day first (adoption.match_answers, `resultFile.linkDays`).
"""
from __future__ import annotations

import hashlib
from typing import Sequence

#: The characters an id is made of (`linkIdChars`).
ALPHABETS = {
    "base32": "0123456789ABCDEFGHJKMNPQRSTVWXYZ",
    "digits": "0123456789",
    "hex": "0123456789ABCDEF",
}
DEFAULT_CHARS = "base32"
DEFAULT_LENGTH = 12
MIN_LENGTH, MAX_LENGTH = 6, 32
#: Days before the day a result file is read for in which a row's id is still looked for (`resultFile.linkDays`).
DEFAULT_LINK_DAYS = 7


def link_id(request_id: str, day: str, system: str, occurrence: int = 1, length: int = DEFAULT_LENGTH,
            chars: str = DEFAULT_CHARS, salt: int = 0, part: int = 0) -> str:
    """
    The id of one request (one time of it; one shot of a time shared out,
    `part` 2 …) handed to `system` for `day` (`2026-11-12`); `salt` works
    out another.
    """
    alphabet = ALPHABETS.get(chars, ALPHABETS[DEFAULT_CHARS])
    length = min(max(int(length), MIN_LENGTH), MAX_LENGTH)
    text = ("\x1f".join((request_id, day, system, str(max(1, int(occurrence))))) + (f"\x1fP{int(part)}" if part > 1 else "")
            + (f"\x1f{salt}" if salt else ""))
    n = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
    out = []
    for _ in range(length):
        n, r = divmod(n, len(alphabet))
        out.append(alphabet[r])
    return "".join(out)


def link_ids(cands: Sequence, day: str, system) -> dict:
    """
    The ids of the requests taken for one system on a day (daily.Candidate,
    `system` its ExportSystem): {Candidate.key: id}. Worked out in key order,
    a second request whose id is already used gets another (`salt`), so that
    no two of one day's file share one; the same candidates give the same ids
    wherever they are worked out (the file, the history).
    """
    from .daily import TAKEN

    mine = sorted({c.key: c for c in cands if c.result == TAKEN and c.system is not None and c.system.system == system.system}.values(),
                  key=lambda c: (c.request.id, c.occurrence, c.part))
    used: set = set()
    out: dict = {}
    for c in mine:
        salt = 0
        while True:
            shot = {"part": c.part} if c.part > 1 else {}
            value = link_id(c.request.id, day, system.system, c.occurrence, system.link_length, system.link_chars, salt, **shot)
            if value not in used:
                break
            salt += 1
        used.add(value)
        out[c.key] = value
    return out
