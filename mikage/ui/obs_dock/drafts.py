"""
観測記録ドックの送信待ち (ui/obs_dock, docs/architecture.md 段階7): the records saved while their server could not be
reached. A part of ObservationDock (ui/observation_dock.py): its methods as they were, on the dock's
widgets and state.
"""
from __future__ import annotations

from typing import Optional


class DraftsPart:
    """観測記録ドックの送信待ち."""

    def waiting_count(self) -> tuple[int, int]:
        """This store's items in 送信待ち: (all of them, the records not saved among them)."""
        book, work = self.book, self.context.offline
        if book is None:
            return 0, 0
        drafts = len(book.drafts())
        labels = [("observation", book.store.label)]
        area_label_ = getattr(book.area_store, "label", None)
        if area_label_:
            labels.append(("area", area_label_))
        others = sum(len(work.pending(kind, label)) for kind, label in labels)
        return drafts + others, drafts

    def render_drafts(self) -> None:
        """The 送信待ち count on the dock (the status bar's list changed)."""
        try:
            n, drafts = self.waiting_count()
        except RuntimeError:  # closed
            return
        self.drafts_button.setText(self.tr("送信待ち {n} 件…").format(n=n))
        # Orange while records not saved wait (to be sent or looked at); blue for the work kept offline only.
        self.drafts_button.setStyleSheet("QPushButton { color: %s; font-weight: bold; }" % ("#b45309" if drafts else "#1d4ed8"))
        self.drafts_button.setVisible(n > 0)

    def open_drafts(self):
        """「送信待ち」: the one list of what is not on the server yet (the status bar's)."""
        return self.open_outbox() if self.open_outbox is not None else None

    def open_draft(self, draft_id: str, store: Optional[str] = None) -> bool:
        """
        A kept record opened in the form again, on its image (which must be
        open): a change on the record as it is now, with what was typed.
        `store`: its store, chosen in the dock first when another one is.
        """
        if store and (self.book is None or self.book.store.label != store):
            i = self.store_select.findText(store)
            if i < 0:
                self.say(self.tr("保存先「{s}」が見つからない。").format(s=store))
                return False
            self.store_select.setCurrentIndex(i)
        book = self.book
        d = book.draft(draft_id) if book else None
        if d is None:
            return False
        entry = next((e for e in book.images if e.id == d.record.image_id), None)
        if entry is None:
            self.say(self.tr("先に画像 {n} を開いてから、フォームで開く。").format(n=d.record.image_name or d.record.image_id))
            return False
        if entry is not book.current:
            book.current = entry
            self._went()
        editing = book.record(d.editing_id) if d.editing else None
        if d.editing and editing is None:
            self.say(self.tr("{n} の元の記録が見つからない。削除されたか、まだ取得していない。").format(n=d.record.object_name))
            return False
        source = book.record(d.source_id) if d.source_id else None
        r = d.record
        self.show()
        self.raise_()
        self.open_form(r.lon, r.lat, editing=editing, source=source)
        v = d.values
        self.object_input.setText(v.get("object", ""))
        self.kind_input.setText(v.get("kind", ""))
        self.activity_input.setText(v.get("activity", ""))
        self.note_input.setText(v.get("note", ""))
        self._fill_confidence(v.get("confidence", ""))
        for edit, key in ((self.length_input, "length"), (self.heading_input, "heading"), (self.width_input, "width")):
            edit.setText(v.get(key, ""))
            if v.get(key):
                edit.setVisible(True)
        self._draft_id = d.id
        self._filled = None  # typed (not what the record says): never filled again by a refresh
        self._center(r)
        return True
