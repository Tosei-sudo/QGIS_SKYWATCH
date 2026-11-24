"""
The 「メモ」 column of the lists of requests (tsudo, 2026-11-24: 「撮像依頼系
のテーブル画面に、メモ列を表示できるようにしてほしい。通常は非表示で」):
撮像依頼, 撮像予定, 撮像計画ファイル出力 and 計画と実績の照合 each show the
request's own メモ (the one of its form).

Hidden until shown from the header's right-click menu or 「列の表示と順番…」
(ui/list_views.py `hidden`); a note is put on one line (its line breaks as
spaces) and cut after its first characters, the whole note being the cell's
tip.
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtGui import QStandardItem

from .docks import plain_tip

#: The column's title.
MEMO = "メモ"
#: The characters of a note the cell shows (the rest: 「…」 and the tip).
MEMO_CHARS = 40


def memo_text(note: Optional[str], chars: int = MEMO_CHARS) -> str:
    """A note on one line (its lines joined with a space), its first `chars` characters then 「…」 when longer."""
    line = " ".join(part.strip() for part in (note or "").splitlines() if part.strip())
    return line if len(line) <= chars else line[:chars].rstrip() + "…"


def memo_item(note: Optional[str]) -> QStandardItem:
    """The cell of a note: the note on one line, the whole note as its tip (shown as written, never as HTML)."""
    item = QStandardItem(memo_text(note))
    if note and note.strip():
        item.setToolTip(plain_tip(note.strip()))
    return item
