"""
画像カタログドックの検索と一覧 (ui/catalog_parts, docs/architecture.md): searching, reading on with 「続きを読む」, the count and the list
of images found. A part of CatalogDock (ui/catalog_dock.py): its methods as they were, on the dock's widgets and
state.
"""
from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt

from ...core.catalog.api import CatalogConfig, CatalogLayerInfo, CatalogRecord, read_catalog_layer, search_catalog
from ...core.obs.api import AssignmentRepository
from ...qgis_io.tasks import run_task
from .common import _Listed, _key


class SearchPart:
    """画像カタログドックの検索と一覧."""

    def _start(self, description: str, work, done, failed=None) -> None:
        def fail(message: str) -> None:
            self.search_button.setEnabled(True)
            self.status.setText(self.tr("{what}に失敗: {m}").format(what=description, m=message))

        task = run_task(description, work, done, failed or fail)
        self._tasks.append(task)
        task.taskCompleted.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)
        task.taskTerminated.connect(lambda: self._tasks.remove(task) if task in self._tasks else None)

    def _layer_of(self, client, catalog: CatalogConfig, token) -> CatalogLayerInfo:
        info = self.layers.get(catalog.url)
        if info is None:
            info = read_catalog_layer(client, catalog, token)
            self.layers[catalog.url] = info
        return info

    def search(self) -> None:
        """Searches the chosen catalog (images like the one compared with, during 「類似画像の検索」)."""
        problem = self.input_problem()
        if problem:
            self.status.setText(problem)
            return
        if self.reference is not None:
            self._search_similar()
            return
        catalog = self.catalog()
        if catalog is None:
            return
        search = self.conditions()
        box = self.area()
        if self.area_box.currentData() == "drawn" and box is None:
            self.status.setText(self.tr("範囲が未作成。「範囲を描く」で地図に描画"))
            return
        if self.area_box.currentData() == "saved" and box is None:
            self.status.setText(self._no_saved_area())
            return
        if self.area_box.currentData() == "layer" and box is None:
            layer = self.area_layer.currentLayer()
            if layer is None:
                self.status.setText(self.tr("範囲のレイヤーが未選択"))
            elif self.selected_only.isChecked():
                self.status.setText(self.tr("「{name}」に選択地物なし").format(name=layer.name()))
            else:
                self.status.setText(self.tr("「{name}」に形のある地物なし").format(name=layer.name()))
            return
        self._read(_Listed(catalog, search, box))

    def read_more(self) -> None:
        """「続きを読む」: the next `maxResults` images of the search listed added to the list."""
        listed = self._listed
        if listed is None or listed.next_offset is None:
            return
        self._read(listed, more=True)

    def _read(self, listed: _Listed, more: bool = False) -> None:
        """Reads the newest images of `listed` (a new search; in a task), or with `more` the next ones."""
        catalog = listed.catalog
        client = self.context.esri()
        token = None
        offset = (listed.next_offset or 0) if more else 0
        similar = listed.rank is not None
        if not more:
            self._listed = listed
            self.more_button.hide()

        def work(task):
            # Read here (once) so that opening a record's folder finds the imagePatterns of a sensor's other names.
            self.context.satellite_names(client, task.isCanceled)
            return search_catalog(client, catalog, self._layer_of(client, catalog, token), listed.search, listed.box, token,
                                  offset, task.isCanceled)

        def done(result) -> None:
            self._reading(False)
            if listed is not self._listed or (similar and self.reference is None):
                return  # searched again since (another list shown, 「類似画像の検索」 left)
            listed.total, listed.next_offset = result.total, result.next_offset
            known = {_key(r) for r in listed.read}
            new = [r for r in result.records if _key(r) not in known]
            listed.read += new
            if similar:
                chosen = self.chosen() if more else []
                records, overlaps = listed.rank(listed.read)
                self.show_records(catalog, records, overlaps, listed)
                self.choose(chosen)
                self._show_count()
            elif more:
                self._add_records(new)
            else:
                self.show_records(catalog, new, listed=listed)
                self._show_count()

        def failed(message: str) -> None:
            self._reading(False)
            what = self.tr("続きの取得") if more else self.tr("類似画像の検索") if similar else self.tr("画像カタログの検索")
            self.status.setText(self.tr("{what}に失敗: {m}").format(what=what, m=message))

        self._reading(True)
        self.status.setText(self.tr("続きを取得中") if more else self.tr("類似画像の検索中") if similar else self.tr("検索中"))
        self._start(self.tr("画像カタログの続き") if more else self.tr("類似画像の検索") if similar else self.tr("画像カタログの検索"),
                    work, done, failed)

    def _reading(self, on: bool) -> None:
        """A search (or its 続き) started (`on`) or ended: while any is read, neither button asks again."""
        self._reads = max(0, self._reads + (1 if on else -1))
        idle = self._reads == 0
        self.search_button.setEnabled(idle and bool(self.catalogs()))
        self.more_button.setEnabled(idle)

    def _show_count(self) -> None:
        """How many images the list has of how many match, and 「続きを読む」 when there are more."""
        listed = self._listed
        n = len(self.model.records)
        if listed is not None and listed.rank is not None:
            text = self.tr("類似画像 {n} 件").format(n=n)
            if listed.next_offset is not None:
                text += (self.tr("（候補 {total} 件中 新しい {read} 件から）").format(total=listed.total, read=len(listed.read))
                         if listed.total is not None else self.tr("（候補の新しい {read} 件から）").format(read=len(listed.read)))
        elif listed is not None and listed.total is not None and listed.total > n:
            text = self.tr("{total} 件中 新しい {n} 件").format(total=listed.total, n=n)
        elif listed is not None and listed.next_offset is not None:
            text = self.tr("新しい {n} 件").format(n=n)
        else:
            text = self.tr("{n} 件").format(n=n)
        self.status.setText(text)
        self._show_more()

    def _show_more(self) -> None:
        """「続きを読む（次の n 件）」 while the search listed has more images."""
        listed = self._listed
        if listed is None or listed.next_offset is None:
            self.more_button.hide()
            return
        n = listed.catalog.max_results
        if listed.total is not None:
            n = max(1, min(n, listed.total - listed.next_offset))
        self.more_button.setText(self.tr("続きを読む（次の {n} 件）").format(n=n))
        self.more_button.show()

    def _add_records(self, records) -> None:
        """
        The images read on added to the list in its order (newest first, or by
        the heading clicked), the rows chosen and where the list is scrolled to kept.
        """
        bar = self.table.verticalScrollBar()
        scrolled = bar.value()
        self.model.add_records(records)
        self._show_count()
        header = self.table.horizontalHeader()
        section = header.sortIndicatorSection()
        if 0 <= section < self.model.columnCount():
            # The rows follow their images (the selection too); the map is drawn again and 「未着手のみ」 applied.
            self.model.sort(section, header.sortIndicatorOrder())
        else:
            self._redraw()
            self._apply_work_filter()
        bar.setValue(scrolled)
        self.load_work()
        self.load_ratings(new_only=True)

    def show_records(self, catalog: CatalogConfig, records, overlaps=None, listed: Optional[_Listed] = None) -> None:
        """Lists `records` of `catalog`; `listed`: the search they are of (「続きを読む」), None for other lists (an event's images)."""
        self._shown_catalog = catalog
        self._listed = listed
        self._show_more()
        self._show_file_buttons()
        self.model.set_records(catalog, records, overlaps, work=self.context.config.assignments is not None,
                               reviews=self.context.config.image_reviews is not None)
        # The list comes newest first (類似画像: the most alike first): the heading says so without sorting again.
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        acquired = next((i for i, c in enumerate(self.model.columns) if c.key == "acquired"), -1)
        header.setSortIndicator(acquired if overlaps is None else -1, Qt.SortOrder.DescendingOrder)
        header.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._redraw()
        self.load_work()
        self.load_ratings()

    def forget_list(self) -> None:
        """The list emptied, its footprints off the map (練習モード started or ended: its images are of the other server)."""
        self.reference = None
        self.similar.hide()
        self._shown_catalog = None
        self._listed = None
        self._show_more()
        self.model.set_records(None, [])
        self._redraw()
        self.status.setText("")

    @property
    def records(self) -> list[CatalogRecord]:
        return list(self.model.records)

    def load_work(self) -> None:
        """Reads who works the listed images (`assignments`; in a task): the 分担 column and 「未着手のみ」."""
        source = self.context.config.assignments
        if source is None or self.model.work is None or not self.model.records:
            self._work_timer.stop()
            return
        if self.isVisible() and not self._work_timer.isActive():
            self._work_timer.start()
        from ...core.obs.api import work_state

        self._work_asked += 1
        asked = self._work_asked
        ids = [r.id for r in self.model.records if r.id]
        repo = AssignmentRepository(self.context.esri(), source, None)

        def work(_task):
            rows = repo.assignments_of_images(ids)
            return {i: work_state(rows.get(i, [])) for i in ids}

        def done(states) -> None:
            if asked != self._work_asked:
                return  # listed again since
            self.model.set_work(states)
            self._apply_work_filter()

        def failed(message: str) -> None:
            if asked == self._work_asked:
                self.status.setText(self.tr("分担の取得に失敗: {m}").format(m=message))

        self._start(self.tr("分担の取得"), work, done, failed)

    def work_changed(self, image_id: str) -> None:
        """
        An image's 分担 written in 観測記録 (taken, 完了, given back): its row's
        分担 read again at once, not at the next search or every 3 minutes
        (操作性点検 7, tsudo 2026-11-23). Nothing when it is not listed.
        """
        source = self.context.config.assignments
        if source is None or self.model.work is None or not any(r.id == image_id for r in self.model.records):
            return
        from ...core.obs.api import work_state

        asked = self._work_asked
        repo = AssignmentRepository(self.context.esri(), source, None)

        def work(_task):
            return work_state(repo.assignments_of_images([image_id]).get(image_id, []))

        def done(state) -> None:
            if asked != self._work_asked or self.model.work is None:
                return  # listed again since (read with the others)
            self.model.set_work({**self.model.work, image_id: state})
            self._apply_work_filter()

        self._start(self.tr("分担の取得"), work, done, lambda _message: None)

    def load_ratings(self, new_only: bool = False) -> None:
        """
        Reads the 画像の評価 of the listed images (`imageReviews`; in a task):
        the 評価 column. `new_only`: those not read yet (「続きを読む」).
        """
        source = self.context.config.image_reviews
        if source is None or self.model.reviews is None or not self.model.records:
            return
        from ...core.image_reviews import load_reviews

        known = self.model.reviews if new_only else {}
        ids = [r.id for r in self.model.records if r.id and r.id not in known]
        if not ids:
            return
        self._ratings_asked += 1
        asked = self._ratings_asked
        client, token = self.context.esri(), None

        def work(_task):
            found = load_reviews(client, source, ids, token)
            return {i: found.get(i, []) for i in ids}

        def done(found) -> None:
            if asked != self._ratings_asked or self.model.reviews is None:
                return  # listed again since
            self.model.set_reviews({**known, **found})

        def failed(message: str) -> None:
            if asked == self._ratings_asked:
                self.status.setText(self.tr("画像の評価の取得に失敗: {m}").format(m=message))

        self._start(self.tr("画像の評価の取得"), work, done, failed)

    def rating_changed(self, image_id: str) -> None:
        """An image's 画像の評価 saved in 観測記録: its row's 評価 read again at once (nothing when it is not listed)."""
        source = self.context.config.image_reviews
        if source is None or self.model.reviews is None or not any(r.id == image_id for r in self.model.records):
            return
        from ...core.image_reviews import load_reviews

        asked = self._ratings_asked
        client, token = self.context.esri(), None

        def done(rows) -> None:
            if asked != self._ratings_asked or self.model.reviews is None:
                return
            self.model.set_reviews({**self.model.reviews, image_id: rows})

        self._start(self.tr("画像の評価の取得"), lambda _t: load_reviews(client, source, [image_id], token).get(image_id, []),
                    done, lambda _message: None)

    def _apply_work_filter(self) -> None:
        """「未着手のみ」: the rows of images someone took (or not read yet) hidden."""
        from ...core.obs.api import UNWORKED

        only = self.unworked_only.isChecked() and self.model.work is not None
        shown = 0
        for row, r in enumerate(self.model.records):
            state = self.model.work_state(r)
            hide = only and (state is None or state[0] != UNWORKED)
            self.table.setRowHidden(row, hide)
            shown += not hide
        base = self.status.text().split(self.tr("（表示: 未着手"))[0]
        self.status.setText(base + (self.tr("（表示: 未着手 {n} 件）").format(n=shown) if only and self.model.records else ""))

    def normal_search(self) -> None:
        """「通常の検索に復帰」: leaves 「類似画像の検索」 and searches with the form."""
        self.reference = None
        self.similar.hide()
        self.search()
