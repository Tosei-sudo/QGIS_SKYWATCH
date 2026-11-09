"""
「見本で記録」 of the 観測記録 dock (tsudo, 2026-11-09: many alike things
lined up): one thing recorded is the 見本, and the rest are put down as it
(its 種別・活動・確度 and size), named in turn.

- 連続 (S): a double click records one where it is, lying as the 見本; a
  press and drag records one turned to the drag (the press is its centre,
  the drag points to its front; tsudo's idea). A single click waits for the
  system's double-click time and then does what it does in the dock (a
  record or a hollow mark opens in the form; elsewhere nothing). A double
  click on a hollow mark records that thing continued (継続).
- 並べる (G): a row by its two ends (the centres of the first and last
  thing), or a grid by a third click where the last row starts; how many
  with the boxes (＋ / − on the map), how they lie (as the 見本, across or
  along the row), a place clicked again is left out; Enter records them all
  in one save.
- 似たものを探す: the things on the image in the map's view that look like
  the 見本 (core.obs.similar, no model), as a layer the dock 「AI結果」 lists
  for checking (正・誤検出) and recording.

The 見本 is the last record saved from the form (or the one open in it,
with 「開いている記録を見本に」); without one, the form's last 種別・活動・確度 at a
point. Backspace takes the last made back (a row as one). What is put down
waits while a save is under way and goes in the next save together, so an
Esri store gets one request for many clicks.

The panel follows the book (tsudo, 2026-11-09: 「消したとき連続作成の方に反映
されない」): the 見本 shows its record as it is now, and when that record is
deleted (from the list, by 取り消し, by someone else) it is no longer the
見本; this image's newest record takes its place, else none. 取り消し forgets
records deleted meanwhile, so Backspace always takes back something still
there.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QWidget,
)

from ..core.obs.flows import ends_of, outline_of
from ..core.obs.geo import bearing, km_text
from ..core.obs.similar import ANGLES_ALL, ANGLES_NEAR, Cancelled, find_similar
from ..core.obs.stamps import MOST, Layout, Sample, StampEntry, default_count, entry_of, sample_of, sample_text
from ..core.obs.taxonomy import path_of
from ..qgis_io.obs_images import image_layers, layer_at
from ..qgis_io.tasks import run_task
from .docks import plain_label
from .last_inputs import InputMemory
from .obs_overlay import Mark, Scene

if TYPE_CHECKING:
    from .observation_dock import ObservationDock

HINT_STAMP = "ダブルクリック: 見本と同じ向きで記録 ／ 押してドラッグ: その向きに回して記録（白抜きの印をダブルクリックで継続）"
HINT_ROW = {
    0: "列の始め（最初の 1 つの中心）をクリック",
    1: "列の終わり（最後の 1 つの中心）をクリック",
    2: "最後の列の始め（最初の 1 つの中心）をクリック",
}
HINT_READY = "数を合わせ（＋ / −）、空いている所はクリックで外して Enter で記録（もう一度クリックで戻す）"
HOWS = [("sample", "見本のまま"), ("across", "列に直角"), ("along", "列に沿う")]


def tr(text: str) -> str:
    return QCoreApplication.translate("Skywatch", text)


class ManyPanel(QGroupBox):
    """「見本で記録」 under the recording row (shown while 連続 or 並べる is on)."""

    def __init__(self, dock: "ObservationDock"):
        super().__init__(tr("見本で記録"))
        self.dock = dock
        #: '' (the form), 'stamp' (連続) or 'row' (並べる).
        self.mode = ""
        self.sample: Optional[Sample] = None
        #: (lon, lat, heading) while a press is dragged round (連続).
        self.turning: Optional[tuple] = None
        self.layout_ = Layout()
        #: Things put down and not sent yet: (image id, entry, group), sent together in the next save.
        self.waiting: list[tuple[str, StampEntry, int]] = []
        self._lock = threading.Lock()
        self._sending = False
        self._group = 0
        #: What each put-down made (record ids), the last last, for 「取り消し」: (image id, ids).
        self.made: list[tuple[str, list[str]]] = []
        self.searching = False
        self._build()
        self.hide()

    # ---- building ------------------------------------------------------

    def _build(self) -> None:
        f = QFormLayout(self)
        self.sample_label = plain_label(QLabel())
        self.sample_label.setWordWrap(True)
        f.addRow(tr("見本"), self.sample_label)
        self.pick_button = self._button(tr("開いている記録を見本に"), tr("フォームで開いている記録（白抜きの印なら元の記録）を見本に。"
                                                                    "ふだんはフォームで最後に記録した記録が見本"), self.use_open_record)
        f.addRow(self.pick_button)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        f.addRow(self.hint)

        # 並べる
        self.row_box = QWidget()
        r = QFormLayout(self.row_box)
        r.setContentsMargins(0, 0, 0, 0)
        shape = QHBoxLayout()
        self.shape_select = QComboBox()
        self.shape_select.addItem(tr("列"), "row")
        self.shape_select.addItem(tr("格子"), "grid")
        self.shape_select.setToolTip(tr("列: 両端の 2 クリック ／ 格子: さらに最後の列の始めをクリック（平行四辺形も可）"))
        self.shape_select.currentIndexChanged.connect(lambda _i: self._shape_changed())
        self.how_select = QComboBox()
        for how, label in HOWS:
            self.how_select.addItem(tr(label), how)
        self.how_select.setToolTip(tr("並べるものの向き（列に直角・列に沿う は、2 つの向きのうち見本に近い方）"))
        self.how_select.currentIndexChanged.connect(lambda _i: self._layout_changed())
        shape.addWidget(self.shape_select)
        shape.addWidget(QLabel(tr("向き")))
        shape.addWidget(self.how_select, 1)
        r.addRow(tr("形"), shape)
        counts = QHBoxLayout()
        self.columns_input = QSpinBox()
        self.columns_input.setRange(1, MOST)
        self.columns_input.setValue(2)
        self.columns_input.setToolTip(tr("1 列の数（地図にフォーカスがあるときは ＋ / −）"))
        self.columns_input.valueChanged.connect(lambda _v: self._layout_changed())
        self.rows_input = QSpinBox()
        self.rows_input.setRange(1, MOST)
        self.rows_input.setValue(2)
        self.rows_input.setToolTip(tr("列の数（格子）"))
        self.rows_input.valueChanged.connect(lambda _v: self._layout_changed())
        self.rows_label = QLabel(tr("列数"))
        counts.addWidget(self.columns_input)
        counts.addWidget(self.rows_label)
        counts.addWidget(self.rows_input)
        counts.addStretch(1)
        r.addRow(tr("1 列の数"), counts)
        self.spacing_label = QLabel()
        r.addRow(self.spacing_label)
        actions = QHBoxLayout()
        self.row_submit = self._button(tr("並べて記録（Enter）"), "", self.submit_row)
        actions.addWidget(self.row_submit)
        actions.addWidget(self._button(tr("やり直し"), tr("並べる位置のクリックをやり直す"), self.restart_row))
        actions.addStretch(1)
        r.addRow(actions)
        f.addRow(self.row_box)

        # 似たものを探す
        find = QHBoxLayout()
        self.find_button = self._button(tr("似たものを探す"), tr(
            "地図の表示範囲で見本に似たものを画像から探し、「AI結果」に一覧（正・誤検出で確認して取り込み）\n"
            "見本はこの画像の、長さと向きのある記録（2〜4 点で測ったもの）。光学画像で向きのそろった所ほど確実"), self.find_similar)
        self.angles_select = QComboBox()
        self.angles_select.addItem(tr("すべての向き"), "all")
        self.angles_select.addItem(tr("見本に近い向き"), "near")
        self.angles_select.setToolTip(tr("探す向き: すべて（15° ごと）／見本の向きの ±30° と逆向き"))
        self.threshold_input = QDoubleSpinBox()
        self.threshold_input.setRange(0.3, 0.95)
        self.threshold_input.setSingleStep(0.05)
        self.threshold_input.setDecimals(2)
        self.threshold_input.setValue(0.6)
        self.threshold_input.setToolTip(tr("似ている度合いのしきい値（−1〜1、高いほど厳しい）"))
        find.addWidget(self.find_button)
        find.addWidget(self.angles_select)
        find.addWidget(QLabel(tr("しきい値")))
        find.addWidget(self.threshold_input)
        find.addStretch(1)
        f.addRow(find)
        InputMemory("observation_many", {"how": self.how_select, "shape": self.shape_select, "angles": self.angles_select,
                                         "threshold": self.threshold_input}).restore()
        self.memory = InputMemory("observation_many", {"how": self.how_select, "shape": self.shape_select, "angles": self.angles_select,
                                                       "threshold": self.threshold_input}).watch()
        self.layout_.shape = self.shape_select.currentData() or "row"
        self.layout_.how = self.how_select.currentData() or "sample"

        actions = QHBoxLayout()
        self.undo_button = self._button(tr("1 つ取り消し（Backspace）"), tr("最後に連続・並べるで記録したものを取り消す（並べたものはまとめて）"), self.undo)
        actions.addWidget(self.undo_button)
        actions.addStretch(1)
        f.addRow(actions)

    def _button(self, text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _on=False: slot())
        return b

    # ---- the mode --------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """'stamp' (連続), 'row' (並べる) or '' (each record by the form)."""
        mode = mode if mode in ("stamp", "row") else ""
        self.mode = mode
        self.turning = None
        self.restart_row(quiet=True)
        if mode:
            if self.sample is None:
                self.sample = self._newest_here()
        self.setVisible(bool(mode))
        self.row_box.setVisible(mode == "row")
        self.render()
        if mode == "stamp":
            self.dock._say(tr(HINT_STAMP))
        elif mode == "row":
            self.dock._say(tr(HINT_ROW[0]))

    @property
    def stamping(self) -> bool:
        return self.mode == "stamp" and self.dock.active and not self.dock.area_mode

    @property
    def rowing(self) -> bool:
        return self.mode == "row" and self.dock.active and not self.dock.area_mode

    def _book(self):
        return self.dock.book

    def _newest_here(self) -> Optional[Sample]:
        """This image's newest record (the one most likely meant), else none."""
        book = self._book()
        image = book.current if book else None
        if image is None:
            return None
        rows = [o for o in book.living() if o.image_id == image.id and o.present]
        return sample_of(max(rows, key=lambda o: o.updated_at)) if rows else None

    def _sample(self) -> Sample:
        """The 見本, else the form's last values (at a point)."""
        if self.sample is not None:
            return self.sample
        book = self._book()
        last = book.last if book else None
        kinds = book.vocabulary.kinds if book else []
        return Sample(kind=(last.kind if last and last.kind else (kinds[0] if kinds else "")),
                      activity=last.activity if last else "", confidence=last.confidence if last else "")

    def use_record(self, record) -> None:
        """A record as the 見本 (saved from the form, or 「見本に」)."""
        if record is None or not record.present:
            return
        self.sample = sample_of(record)
        self.render()

    def reset(self) -> None:
        """Another store: what waits for it and what can be taken back are dropped, and so is the 見本."""
        with self._lock:
            self.waiting = []
        self._sending = False
        self.made = []
        self.sample = None
        self.turning = None
        self.restart_row(quiet=True)

    def use_open_record(self) -> None:
        """The record open in the form (or the thing carried over) as the 見本."""
        pending = self.dock.pending
        record = (pending[2] or pending[3]) if pending else None
        if record is not None:
            self.use_record(record)
            self.dock._say(tr("見本: {s}").format(s=self.sample_label.text()))

    def follow_book(self) -> None:
        """
        After any change to the book: the 見本 as its record is now (a deleted
        one is not kept: this image's newest record, else none, while a mode
        is on), and what 取り消し can take back without the records deleted
        meanwhile.
        """
        book = self._book()
        if book is None:
            return
        s = self.sample
        if s is not None and s.record_id:
            record = book.record(s.record_id)
            if record is None or record.deleted_at is not None:
                self.sample = self._newest_here() if self.mode else None
            elif record.present:
                self.sample = sample_of(record)
        alive = self._alive()
        made = [(image, [i for i in ids if i in alive]) for image, ids in self.made]
        self.made = [(image, ids) for image, ids in made if ids]
        self.render()

    def _alive(self) -> set:
        book = self._book()
        return {o.id for o in book.records if o.deleted_at is None} if book else set()

    def went(self) -> None:
        """Another image: what was being laid out is dropped; 取り消し keeps to its image."""
        self.turning = None
        self.restart_row(quiet=True)
        self.render()

    def render(self) -> None:
        if self.sample is not None:
            self.sample_label.setText(sample_text(self.sample))
        else:
            kind = (path_of(self._sample().kind) or [tr("種別なし")])[-1] if self._book() else ""
            self.sample_label.setText(tr("なし: 前回の種別（{kind}）の点で記録。1 件をフォームで記録するとそれが見本に").format(kind=kind))
        self.hint.setText(tr(HINT_STAMP) if self.mode == "stamp" else self._row_hint() if self.mode == "row" else "")
        self.undo_button.setEnabled(bool(self._undoable()))
        pending = self.dock.pending
        self.pick_button.setVisible(bool(pending and (pending[2] or pending[3])))
        self.find_button.setEnabled(not self.searching)
        self.find_button.setText(tr("探しています…") if self.searching else tr("似たものを探す"))
        grid = self.layout_.shape == "grid"
        self.rows_label.setVisible(grid)
        self.rows_input.setVisible(grid)
        along, across = self.layout_.spacing()
        parts = []
        if along is not None:
            parts.append(tr("間隔 {d}").format(d=_metres(along)))
        if across is not None:
            parts.append(tr("列の間隔 {d}").format(d=_metres(across)))
        if self.layout_.ready:
            parts.append(tr("{n} 件（外す {k} 件）").format(n=len(self.layout_.kept()), k=len(self.layout_.skipped)))
        self.spacing_label.setText("・".join(parts))
        self.row_submit.setEnabled(self.layout_.ready and bool(self.layout_.kept()))

    def _row_hint(self) -> str:
        n = len(self.layout_.corners)
        return tr(HINT_READY) if self.layout_.ready else tr(HINT_ROW[min(n, 2)])

    # ---- the map ---------------------------------------------------------

    def clicked(self, lon: float, lat: float, mark: Optional[Mark]) -> bool:
        """A click while 連続 or 並べる is on: whether it was taken here (else the dock does as without them)."""
        if self.rowing:
            if mark is not None and mark.role == "slot" and mark.key:
                i = int(mark.key.split(":", 1)[1])
                skipped = set(self.layout_.skipped)
                skipped.symmetric_difference_update({i})
                self.layout_.skipped = frozenset(skipped)
                self.render()
                self.dock.draw()
                return True
            if self.layout_.ready:
                self.restart_row(quiet=True)
            self._corner(lon, lat)
            return True
        if self.stamping:
            if mark is not None and mark.key and mark.role in ("obs", "ghost"):
                return False
            # A single click on nothing: the person meant a double click or a drag.
            self.dock._say(tr(HINT_STAMP))
            return True
        return False

    def double_clicked(self, lon: float, lat: float, mark: Optional[Mark]) -> None:
        if not self.stamping or self._book() is None:
            return
        if mark is not None and mark.role == "ghost" and mark.key:
            book = self._book()
            c = next((x for x in book.carried() if x.source.id == mark.key), None)
            if c is not None:
                image = book.current.id
                object_id = c.source.object_id
                self.dock.do(tr("継続の記録"), lambda b: b.carry(object_id, True),
                             lambda saved: self._made(image, [saved.id]) if saved is not None else None, watch=True)
            return
        if mark is not None and mark.role == "obs" and mark.key:
            self.dock._say(tr("記録の上（重ねては記録しない。少し離してダブルクリック）"))
            return
        self.put([(lon, lat, None)])

    def turned(self, lon0: float, lat0: float, lon: float, lat: float, done: bool) -> None:
        """A press dragged round (連続): the thing at the press, its front towards the pointer."""
        if not self.stamping:
            return
        heading = bearing((lon0, lat0), (lon, lat))
        if not done:
            self.turning = (lon0, lat0, heading)
            self.dock.draw()
            return
        self.turning = None
        self.put([(lon0, lat0, heading)])

    def escape(self) -> bool:
        """Esc: a drag round dropped, the last corner taken back, then the mode ended; whether there was any."""
        if not self.mode:
            return False
        if self.turning is not None:
            self.turning = None
            self.dock.draw()
            return True
        if self.mode == "row" and self.layout_.corners:
            self.layout_.corners = self.layout_.corners[:-1]
            self.layout_.skipped = frozenset()
            self.render()
            self.dock._say(self._row_hint())
            self.dock.draw()
            return True
        self.dock.set_many_mode("")
        return True

    def step(self, by: int) -> bool:
        """＋ / − on the map: one more or fewer in each row (並べる)."""
        if not self.rowing or not self.layout_.corners:
            return False
        self.columns_input.setValue(max(1, self.columns_input.value() + by))
        return True

    def entered(self) -> bool:
        """Enter: the laid-out row recorded (並べる); whether it was."""
        if self.rowing and self.layout_.ready:
            self.submit_row()
            return True
        return False

    # ---- 並べる ----------------------------------------------------------

    def _corner(self, lon: float, lat: float) -> None:
        L = self.layout_
        L.corners = (*L.corners, (lon, lat))
        sample = self._sample()
        if len(L.corners) == 2:
            self._set_count(self.columns_input, default_count(L.corners[0], L.corners[1], sample, L.heading(sample)))
        if L.shape == "grid" and len(L.corners) == 3:
            self._set_count(self.rows_input, default_count(L.corners[0], L.corners[2], sample, L.heading(sample)))
        self._sync()
        self.render()
        self.dock._say(self._row_hint())
        self.dock.draw()

    @staticmethod
    def _set_count(box: QSpinBox, n: int) -> None:
        box.blockSignals(True)
        box.setValue(n)
        box.blockSignals(False)

    def _sync(self) -> None:
        L = self.layout_
        L.columns = self.columns_input.value()
        L.rows = self.rows_input.value()
        L.how = self.how_select.currentData() or "sample"
        L.shape = self.shape_select.currentData() or "row"

    def _shape_changed(self) -> None:
        self._sync()
        if len(self.layout_.corners) > self.layout_.needs:
            self.layout_.corners = self.layout_.corners[: self.layout_.needs]
        self.layout_.skipped = frozenset()
        self.render()
        if self.mode == "row":
            self.dock._say(self._row_hint())
        self.dock.draw()

    def _layout_changed(self) -> None:
        before = (self.layout_.columns, self.layout_.rows)
        self._sync()
        if (self.layout_.columns, self.layout_.rows) != before:
            self.layout_.skipped = frozenset()
        self.render()
        self.dock.draw()

    def restart_row(self, quiet: bool = False) -> None:
        self.layout_.corners = ()
        self.layout_.skipped = frozenset()
        self._sync()
        self.render()
        if not quiet:
            self.dock._say(tr(HINT_ROW[0]))
            self.dock.draw()

    def submit_row(self) -> None:
        L = self.layout_
        if not L.ready:
            self.dock._say(self._row_hint())
            return
        sample = self._sample()
        heading = L.heading(sample)
        places = L.kept()
        if not places:
            self.dock._say(tr("すべて外しています"))
            return
        self.restart_row(quiet=True)
        self.put([(lon, lat, heading) for lon, lat in places])

    # ---- putting down ------------------------------------------------------

    def put(self, places: list[tuple]) -> None:
        """Things put down as the 見本 on this image (saved with what waits, in one save)."""
        book = self._book()
        image = book.current if book else None
        if image is None:
            return
        sample = self._sample()
        self._group += 1
        with self._lock:
            for lon, lat, heading in places:
                self.waiting.append((image.id, entry_of(sample, lon, lat, heading), self._group))
        self._send()
        self.dock.draw()

    def _take(self) -> tuple[str, list[StampEntry], list[int]]:
        """The things of one image waiting (the first one's), taken out to be saved."""
        with self._lock:
            if not self.waiting:
                return "", [], []
            image = self.waiting[0][0]
            mine = [w for w in self.waiting if w[0] == image]
            self.waiting = [w for w in self.waiting if w[0] != image]
        return image, [w[1] for w in mine], [w[2] for w in mine]

    def _send(self) -> None:
        if self._sending or not self.waiting:
            return
        self._sending = True
        taken: dict = {}

        def work(b):
            image, entries, groups = self._take()
            taken.update(image=image, groups=groups, count=len(entries))
            if not entries:
                return []
            return b.add_many(entries, image=image, keep=True, done=f"{len(entries)} 件を見本で記録")

        def after(saved) -> None:
            self._sending = False
            if saved:
                ids = [o.id for o in saved]
                by_group: dict[int, list[str]] = {}
                for g, i in zip(taken.get("groups", []), ids):
                    by_group.setdefault(g, []).append(i)
                for g in sorted(by_group):
                    self._made(taken["image"], by_group[g])
            elif saved is None and taken.get("count"):
                self.dock.iface.messageBar().pushWarning(tr("観測記録"), tr(
                    "見本で記録した {n} 件を保存できませんでした（送信待ちに保管しました）").format(n=taken["count"]))
            self.render()
            self._send()

        def failed(_message: str) -> None:
            self._sending = False
            self.render()

        self.dock.do(tr("見本で記録"), work, after, watch=True, failed=failed)

    def _made(self, image_id: str, ids: list[str]) -> None:
        if ids:
            self.made.append((image_id, ids))
            del self.made[:-50]
        self.render()

    def _undoable(self):
        book = self._book()
        image = book.current.id if book and book.current else None
        if any(w[0] == image for w in self.waiting):
            return "waiting"
        alive = self._alive()
        return next((m for m in reversed(self.made) if m[0] == image and any(i in alive for i in m[1])), None)

    def undo(self) -> bool:
        """The last put down on this image taken back (not yet sent: dropped; saved: deleted); whether there was one."""
        book = self._book()
        image = book.current.id if book and book.current else None
        if image is None:
            return False
        with self._lock:
            mine = [w for w in self.waiting if w[0] == image]
            if mine:
                group = mine[-1][2]
                self.waiting = [w for w in self.waiting if not (w[0] == image and w[2] == group)]
                self.render()
                self.dock._say(tr("見本で記録するところを取り消し"))
                self.dock.draw()
                return True
        # Those deleted meanwhile (from the list, by someone else) are passed over: the last still there is taken back.
        alive = {o.id: o for o in book.records if o.deleted_at is None}
        while True:
            last = next((m for m in reversed(self.made) if m[0] == image), None)
            if last is None:
                self.render()
                self.dock._say(tr("取り消せる記録なし（連続・並べるで記録したもので、まだあるものだけ）"))
                return False
            self.made.remove(last)
            rows = [alive[i] for i in last[1] if i in alive]
            if rows:
                self.dock.remove(rows)
                self.render()
                return True

    # ---- drawing -------------------------------------------------------------

    def paint_into(self, s: Scene, color, cursor: Optional[tuple]) -> None:
        """What is being put down, on the map: the things waiting, the drag round, the row laid out, the 見本 under the pointer."""
        book = self._book()
        image = book.current if book else None
        if image is None or not self.mode:
            return
        sample = self._sample()
        c = color(sample.kind)
        for image_id, e, _g in list(self.waiting):
            if image_id == image.id:
                s.marks.append(Mark("obs", e.lon, e.lat, color(e.kind), "…", ends=ends_of(e.lon, e.lat, e.length, e.heading),
                                    heading=e.heading, outline=outline_of(e.lon, e.lat, e.length, e.width, e.heading)))
        if not self.dock.active:
            return
        if self.mode == "stamp":
            if self.turning is not None:
                lon, lat, heading = self.turning
                s.marks.append(self._slot(lon, lat, heading, sample, c, "", True))
            elif cursor is not None:
                s.marks.append(self._slot(cursor[0], cursor[1], sample.heading, sample, c, "", True))
            return
        L = self.layout_
        heading = L.heading(sample)
        for i, (lon, lat) in enumerate(L.places(cursor)):
            s.marks.append(self._slot(lon, lat, heading, sample, c, f"slot:{i}", i not in L.skipped))
        for p in L.corners:
            s.marks.append(Mark("slot", p[0], p[1], "#e11d48", present=True))

    @staticmethod
    def _slot(lon: float, lat: float, heading: Optional[float], sample: Sample, color: str, key: str, kept: bool) -> Mark:
        return Mark("slot", lon, lat, color if kept else "#9ca3af", present=kept, key=key or None,
                    ends=ends_of(lon, lat, sample.length, heading) if kept else None, heading=heading,
                    outline=outline_of(lon, lat, sample.length, sample.width, heading) if kept else None)

    # ---- 似たものを探す ----------------------------------------------------------

    def _search_sample(self) -> Optional[Sample]:
        """The 見本 for a search: a measured record of this image (the 見本 itself when it is one)."""
        book = self._book()
        image = book.current if book else None
        if image is None:
            return None
        s = self.sample
        if s is not None and s.record_id:
            record = book.record(s.record_id)
            if record is not None and record.image_id == image.id and record.deleted_at is None and s.measured:
                return sample_of(record)
        rows = [o for o in book.living() if o.image_id == image.id and o.present and o.length and o.heading is not None]
        return sample_of(max(rows, key=lambda o: o.updated_at)) if rows else None

    def find_similar(self) -> None:
        from ..qgis_io.similar_io import SimilarError, found_layer, found_places, prepare

        if self.searching:
            return
        book = self._book()
        image = book.current if book else None
        if not image_layers(image):
            self.dock._say(tr("この画像のファイル（レイヤー）が開いていません"))
            return
        sample = self._search_sample()
        if sample is None:
            self.dock._say(tr("見本なし: この画像で 1 つを 2〜4 点（長さ・向き）で記録してから"))
            return
        self.sample = sample
        # A record's images in its folder: searched in the one the 見本 is on.
        layer = layer_at(image, sample.lon, sample.lat) if sample.lon is not None and sample.lat is not None else image_layers(image)[0]
        records = [o for o in book.living() if o.image_id == image.id and o.present]
        canvas = self.dock.canvas
        try:
            pictures = prepare(layer, canvas.extent(), canvas.mapSettings().destinationCrs(), sample, records)
        except SimilarError as error:
            self.dock._say(str(error))
            return
        angles = ANGLES_NEAR if self.angles_select.currentData() == "near" else ANGLES_ALL
        threshold = float(self.threshold_input.value())
        image_id, image_name, image_time = image.id, image.name, image.time

        def work(task):
            def cancelled() -> bool:
                return task.isCanceled()

            try:
                return find_similar(pictures.gray, pictures.valid, pictures.sample_gray, pictures.sample_valid, pictures.sx, pictures.sy,
                                    float(sample.heading), pictures.length_px, pictures.width_px, angles, threshold, pictures.taken,
                                    progress=lambda f: task.progress(f * 100), cancelled=cancelled)
            except Cancelled:
                return None

        def done(found) -> None:
            self.searching = False
            self.render()
            if found is None:
                self.dock._say(tr("似たものを探すのを中止"))
                return
            if not found:
                self.dock._say(tr("見本に似たものは見つかりませんでした（しきい値を下げる・向きを「すべて」に・地図を見本の周りへ）"))
                return
            places = found_places(found, pictures)
            out = found_layer(places, sample, image_id, image_name, image_time)
            QgsProject.instance().addMapLayer(out)
            self.dock._say(tr("見本に似たもの {n} 件（「AI結果」で確認して取り込み）").format(n=len(places)))

        def failed(message: str) -> None:
            self.searching = False
            self.render()
            self.dock._say(tr("似たものを探せませんでした: {m}").format(m=message))

        self.searching = True
        self.render()
        kind = (path_of(sample.kind) or [""])[-1]
        self.dock._say(tr("{kind}に似たものを探しています…").format(kind=kind))
        run_task(tr("似たものを探す"), work, done, failed)


def _metres(m: float) -> str:
    return f"{m:.1f} m" if m < 100 else km_text(m)
