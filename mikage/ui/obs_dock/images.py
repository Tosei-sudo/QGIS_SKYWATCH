"""
観測記録ドックの画像 (ui/obs_dock, docs/architecture.md 段階7): the images recorded on: their list, the one chosen,
its time and satellite, the footprints and layers. A part of ObservationDock (ui/observation_dock.py):
its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional, Sequence

from qgis.core import QgsLayerTree, QgsMapLayerType, QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import QDateTime, QTimer
from qgis.PyQt.QtWidgets import QMessageBox

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from ...core.obs.api import FormValues, ObservationBook, ObservedImage, read_quantity
from ...core.obs.api import SATELLITE_ORIGINS
from ...core.util import read_number
from ...qgis_io.geometry import from_wgs84, to_wgs84
from ...qgis_io.image_order import bring_up
from ...qgis_io.listed_layers import listed_image_of
from ...qgis_io.obs_images import (
    SatelliteLookup, can_mark, image_layers, is_image_layer, open_images, set_manual_time, set_record_on, set_typed_satellite,
)
from ...qgis_io.tasks import run_task
from ..docks import plain_tip
from .common import _ms_of


class ImagesPart:
    """観測記録ドックの画像."""

    def refresh_images(self) -> None:
        """The project's images again."""
        if self.book is None:
            return
        active = self.iface.activeLayer()
        images = open_images(satellites=self._satellite_lookup())
        self.book.set_open_images(images, active.id() if isinstance(active, QgsRasterLayer) else None)
        self.render()
        if any(not i.satellite or i.satellite_origin == "metadata" for i in images):
            # Names to look for in file names and to spell by: read once they are needed (an image outside the catalog).
            self._read_satellite_choices()

    def _satellite_lookup(self) -> SatelliteLookup:
        """What finding the images' 衛星名 needs: the names read so far and the catalogs."""
        return SatelliteLookup(self.context.known_satellite_choices(), tuple(self.context.config.image_catalogs))

    def _read_satellite_choices(self) -> None:
        """
        The satellite names (the image catalogs' sensors, the satellite
        catalogs' names, the alias table) read in the background, once per
        settings file: then the images' 衛星名 are looked for again (file names,
        spelling) and offered in 「衛星不明（入力）」.
        """
        c = self.context.config
        if self._satellites_asked is c or self.context.satellite_choices_read():
            return
        # Once per settings file (one that could not be read whole is not asked again on every change of layers).
        self._satellites_asked = c
        if not (any(x.fields.get("sensor") for x in c.image_catalogs) or c.satellite_catalogs or c.satellite_aliases):
            return
        config = c

        def done(choices) -> None:
            if self.context.config is not config:
                return
            self._fill_satellite_input()
            if choices:
                self.refresh_images()

        run_task(self.tr("衛星名の候補の取得"), lambda task: self.context.satellite_choices(cancelled=task.isCanceled), done,
                 lambda _message: None)

    def _fill_satellite_input(self) -> None:
        """The names offered in 「衛星不明（入力）」 (what is typed there is kept)."""
        typed = self.satellite_input.currentText()
        self.satellite_input.blockSignals(True)
        self.satellite_input.clear()
        self.satellite_input.addItems(self.context.known_satellite_choices().offered())
        self.satellite_input.setCurrentIndex(-1)
        self.satellite_input.setEditText(typed)
        self.satellite_input.blockSignals(False)

    def _image_tip(self, image: Optional[ObservedImage]) -> str:
        """The image chooser's tip: the image, and its 衛星名 with where it came from."""
        if image is None:
            return self.tr("画像")
        if not image.satellite:
            return self.tr("画像: {n}\n衛星名: 不明").format(n=image.name)
        origin = SATELLITE_ORIGINS.get(image.satellite_origin, "")
        return self.tr("画像: {n}\n衛星名: {s}").format(n=image.name, s=image.satellite) + (f"（{origin}）" if origin else "")

    def _layers_changed(self, *_args) -> None:
        self.refresh_images()

    def _image_closing(self, layer_ids) -> None:
        """
        Layers about to go (the layer removed, 「前の画像を閉じる」, a new project): the image of a form with something
        typed among them (tsudo 2026-11-28: with no other layer chosen, the form closed without a word and what was
        typed was lost). What the form says is kept at once in 「保存できなかった観測記録」 (QGIS quit before the
        question: still there, not sent by itself), then 保存 or 破棄 is asked, as when another image is chosen.
        """
        book, was = self.book, self._form_image
        if book is None or was is None or self.pending is None:
            return
        image = next((i for i in book.images if i.id == was.id), was)
        ids = list(image.layer_ids)
        if not ids or not set(ids) <= set(layer_ids):
            return  # listed without its file (it stays), or a file of it stays open
        if not self._typed():
            self.close_form()  # (nothing typed: it goes with its image, other images open or not)
            return
        lon, lat, editing, source = self.pending
        values = self.form_values()
        title = self.form.title()
        kept = book.keep_typed(image, lon, lat, values, editing, source, draft=self._draft_id or None,
                               reason=self.tr("画像を閉じたときに入力中（未記録）"))
        self.close_form()
        self.render_drafts()
        # Asked once QGIS is done removing (a timer of the dock's own: gone with it).
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._ask_closed_image(book, kept.id, image, title))
        timer.timeout.connect(timer.deleteLater)
        timer.start(0)

    def _ask_closed_image(self, book, draft_id: str, image, title: str) -> None:
        """保存 (recorded on the image closed) or 破棄 for what was typed when its image closed (_image_closing)."""
        kept = book.draft(draft_id) if book is self.book else None
        if kept is None:
            return  # sent or thrown away meanwhile, or another store
        answer = self.ask_unsaved(title, image.name, False)
        if answer == "discard":
            book.drop_draft(draft_id)
            self.render_drafts()
            return
        r = kept.record
        values = FormValues(**{k: kept.values.get(k, "") for k in FormValues.__dataclass_fields__})
        try:
            read_quantity(values.quantity)
            for text in (values.length, values.width, values.heading):
                read_number(text)
        except ValueError:
            # (as the form: not saved without the number; it is put right in the form, from the list)
            self.say(self.tr("「{t}」は数値が読めないため「保存できなかった観測記録」に保管（画像を開き直し、一覧からフォームで開いて直す）").format(t=title))
            return
        editing = book.record(kept.editing_id) if kept.editing_id else None
        source = book.record(kept.source_id) if kept.source_id else None
        self.do(self.tr("記録"), lambda b: b.submit(r.lon, r.lat, values, True, editing, source, draft=draft_id, image=image),
                lambda saved: self.render_drafts() if saved is None else self.many.use_record(saved), watch=True)

    def _layer_renamed(self, node, _name: str) -> None:
        """A layer renamed in the layer panel: an image's (not from the catalog: its name shown, obs_images.shown_name)."""
        if self.book is not None and QgsLayerTree.isLayer(node) and any(node.layerId() in i.layer_ids for i in self.book.images):
            self.refresh_images()

    # ---- 「観測記録の画像にする」 (the layer panel's right-click; 操作性点検 1, tsudo 2026-11-23) ----

    def _offer_record_on_action(self) -> None:
        """
        The right-click entry of the rasters that are neither from the catalog
        nor a map a server draws: a background GeoTIFF made an image to record
        on, or a raster with a time made a background (obs_images.is_image_layer).
        """
        self.record_on_action = QAction(self.tr("観測記録の画像にする"), self)
        self.record_on_action.setObjectName("mikageRecordOn")
        self.record_on_action.setCheckable(True)
        self.record_on_action.setToolTip(self.tr(
            "オン: このレイヤーを観測記録の画像にする（撮像日時が読めない画像も）。オフ: 背景として扱い、記録先にしない"))
        self.record_on_action.triggered.connect(self._mark_record_on)
        self.iface.addCustomActionForLayerType(self.record_on_action, "", QgsMapLayerType.RasterLayer, False)
        project = QgsProject.instance()
        project.layersAdded.connect(self._offer_record_on)
        self._offer_record_on(project.mapLayers().values())

    def _withdraw_record_on_action(self) -> None:
        try:
            QgsProject.instance().layersAdded.disconnect(self._offer_record_on)
        except (TypeError, RuntimeError):
            pass
        self.iface.removeCustomActionForLayerType(self.record_on_action)

    def _offer_record_on(self, layers) -> None:
        for layer in layers:
            if can_mark(layer):
                self.iface.addCustomActionForLayer(self.record_on_action, layer)

    def _mark_record_on(self, on: bool) -> None:
        """「観測記録の画像にする」 on the layer chosen: it becomes the image recorded on (on), or a background (off)."""
        layer = self.iface.activeLayer()
        if not can_mark(layer):
            return
        set_record_on(layer, on)
        self.refresh_images()
        if on and self.book is not None and self.book.go_to_layer(layer.id()):
            self._went()

    def _layer_chosen(self, layer) -> None:
        if hasattr(self, "record_on_action"):
            self.record_on_action.setChecked(isinstance(layer, QgsRasterLayer) and is_image_layer(layer))
        if self._following or self.book is None:
            return
        typing_on = self._form_image if self._typed() else None
        if typing_on is not None and layer is not None and layer.id() not in typing_on.layer_ids:
            # Something typed for another image: asked once QGIS is done (a layer removed in the layer panel makes
            # another one chosen while it is still there: 操作性点検の再検証 F10, 「元の画像に戻る」 offered to it).
            layer_id = layer.id()
            QTimer.singleShot(0, lambda: self._layer_chosen_later(layer_id))
            return
        self._follow_layer(layer)

    def _layer_chosen_later(self, layer_id: str) -> None:
        """_layer_chosen with something typed, after QGIS's change: the layer still the one chosen."""
        layer = QgsProject.instance().mapLayer(layer_id)
        active = self.iface.activeLayer()
        if self.book is None or layer is None or active is None or active.id() != layer_id:
            return
        current, typing_on = self.book.current, self._form_image
        if typing_on is not None and current is not None and current.id != typing_on.id and layer_id in current.layer_ids:
            # There already: the form's image was removed, the images read again went to the layer chosen.
            self._went()
            return
        self._follow_layer(layer)

    def _follow_layer(self, layer) -> None:
        """The image of the layer chosen in the layer panel recorded on (a footprint's: the image without its file)."""
        listed = listed_image_of(layer)
        if listed is not None and layer.id() in self.footprints.layer_ids():
            # An image without its file chosen by its footprint's layer.
            current = self.book.current
            if (current is None or current.id != listed) and self.book.go_to(listed):
                self._went()
            return
        if not isinstance(layer, QgsRasterLayer):
            return
        current = self.book.current
        if current is not None and layer.id() in current.layer_ids:
            # Another of the record's images (its folder's): still the same image, the form stays open.
            return
        if self.book.go_to_layer(layer.id()):
            self._went()

    def _went(self) -> bool:
        """
        Another image: the form closes, the layer is chosen and drawn over the
        other images, 「この画像のみ」 follows; 「クリック記録」 goes on once the
        image is taken (分担). Something typed in the form not saved yet is
        asked about first (keep_typing): False when the user went back to
        the form's image (nothing else done).
        """
        if self._keep_typing():
            return False
        self.close_form()
        self._clear_points()
        self.many.went()
        image = self.book.current if self.book else None
        if self.active and self.assignment_bar.needs_claim(image):
            self.set_active(False)
            self.set_active(True, quiet=True)
        # An image without its file: its footprint's layer (made now when just listed).
        self._show_footprints()
        footprint = self.footprints.layer_of(image.id) if image is not None and not image.file else None
        layers = image_layers(image) or ([footprint] if footprint is not None else [])
        if layers:
            layer = layers[0]
            active = self.iface.activeLayer()
            if active is None or active.id() not in {*image.layer_ids, *(x.id() for x in layers)}:
                self._following = True
                try:
                    self.iface.setActiveLayer(layer)
                finally:
                    self._following = False
            if image is not None and image.file and self.isVisible():
                # Once what chose it is done (a click in the layer panel moves no node under it).
                QTimer.singleShot(0, lambda image_id=image.id: self._bring_up(image_id))
        self._apply_only()
        self.render()
        self.refresh()
        return True

    def _bring_up(self, image_id: str) -> None:
        """
        The image recorded on drawn over the other images that cover it
        (image_order.bring_up; 操作性点検 10, tsudo 2026-11-23): the map shows
        the image the marks go on. The layer chosen stays chosen.
        """
        book = self.book
        image = book.current if book else None
        if image is None or image.id != image_id or not image.file or not self.isVisible():
            return
        others = [layer_id for i in book.images if i.id != image.id for layer_id in i.layer_ids]
        active = self.iface.activeLayer()
        self._following = True
        try:
            if bring_up(image.layer_ids, others) and active is not None and self.iface.activeLayer() is not active:
                self.iface.setActiveLayer(active)
        finally:
            self._following = False

    def _typed(self) -> bool:
        """Whether the form open has something typed since it opened (a record kept unsaved and opened again: always)."""
        return self.pending is not None and (self._filled is None or self.form_values() != self._filled)

    def _keep_typing(self) -> bool:
        """
        The image changed with something typed in the form for another one
        (操作性点検 2, tsudo 2026-11-23: it closed without a word): asked 保存
        (on the form's image, then on), 破棄 (then on) or 元の画像に戻る (the
        form's image chosen again, the form open as it was: True).
        """
        book, was = self.book, self._form_image
        image = book.current if book else None
        if book is None or was is None or (image is not None and image.id == was.id) or not self._typed():
            return False
        can_return = any(i.id == was.id and self._still_there(i) for i in book.images)
        answer = self.ask_unsaved(self.form.title(), was.name, can_return)
        if answer == "save":
            self.submit(image=next((i for i in book.images if i.id == was.id), was))
            if self.pending is None:
                return False
            answer = "back"  # (a number that cannot be read: the form stays open, on its image)
        if answer != "back" or not can_return:
            return False
        book.go_to(was.id)
        layers = image_layers(book.current)
        active = self.iface.activeLayer()
        if layers and (active is None or active.id() not in book.current.layer_ids):
            self._following = True
            try:
                self.iface.setActiveLayer(layers[0])
            finally:
                self._following = False
        self.render()
        return True

    def _still_there(self, image) -> bool:
        """Whether an image can be gone back to: one listed without its file, or with a layer in the layer panel."""
        ids = list(image.layer_ids)
        if not ids:
            return True
        root = QgsProject.instance().layerTreeRoot()
        return any(root.findLayer(x) is not None for x in ids)

    def ask_unsaved(self, title: str, image_name: str, can_return: bool) -> str:
        """「保存」「破棄」「元の画像に戻る」 for what is typed in the form: 'save', 'discard' or 'back' (also Esc)."""
        lines = [self.tr("入力中の「{t}」は未保存").format(t=title),
                 self.tr("保存: 元の画像（{n}）に記録してから移る").format(n=image_name),
                 self.tr("破棄: 入力を捨てて移る")]
        if can_return:
            lines.append(self.tr("元の画像に戻る: 移らずに入力を続ける"))
        return self._ask_about_typed(lines, self.tr("元の画像に戻る") if can_return else "")

    def _form_holds(self, editing=None, source=None) -> bool:
        """Whether the form open is that record's (`editing`) or that thing's carried over (`source`) already."""
        if self.pending is None:
            return False
        _lon, _lat, open_editing, open_source = self.pending
        return (editing is not None and open_editing is not None and open_editing.id == editing.id) or (
            source is not None and open_source is not None and open_source.id == source.id)

    def _named(self, o) -> str:
        """A record as asked about (_let_form_go): its thing's name, else its kind."""
        return o.object_name or o.kind or self.tr("記録")

    def _let_form_go(self, doing: str) -> bool:
        """
        Something else about to take the form's place or end it, `doing` (操作性点検の再検証 R2〜R7, tsudo
        2026-11-23: a map click elsewhere, a mark, 「クリック記録」 off, 確認・不可範囲, the store, 連続 closed it
        without a word): with something typed, 保存 (on the form's image), 破棄 or 入力に戻る asked first.
        False: the form stays as it is and nothing else is done (入力に戻る, or a number that cannot be read).
        """
        if self.pending is None or not self._typed():
            return True
        answer = self.ask_leave(self.form.title(), doing)
        if answer == "save":
            self.submit(image=self._form_image)
            return self.pending is None
        if answer == "back":
            self.raise_()
            self.reveal(self.form)
            return False
        return True

    def ask_leave(self, title: str, doing: str) -> str:
        """「保存」「破棄」「入力に戻る」 before `doing` (「クリック記録を止める」…): 'save', 'discard' or 'back' (also Esc)."""
        lines = [self.tr("入力中の「{t}」は未保存").format(t=title),
                 self.tr("保存: 記録してから{d}").format(d=doing),
                 self.tr("破棄: 入力を捨てて{d}").format(d=doing),
                 self.tr("入力に戻る: {d}のをやめ、入力を続ける").format(d=doing)]
        return self._ask_about_typed(lines, self.tr("入力に戻る"))

    def _ask_about_typed(self, lines: list, back_label: str) -> str:
        """The question of ask_unsaved and ask_leave: 保存, 破棄 and `back_label` (none: not offered; Esc saves)."""
        box = QMessageBox(QMessageBox.Icon.Question, self.tr("観測記録"), plain_tip("\n".join(lines)), parent=self)
        save = box.addButton(self.tr("保存"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(self.tr("破棄"), QMessageBox.ButtonRole.DestructiveRole)
        back = box.addButton(back_label, QMessageBox.ButtonRole.RejectRole) if back_label else None
        box.setDefaultButton(back or save)
        box.setEscapeButton(back or save)
        box.exec()
        clicked = box.clickedButton()
        return "save" if clicked is save else "back" if back is not None and clicked is back else "discard" if clicked is not None else "back"

    def toggle_recording(self) -> None:
        """Starts recording, or ends it (「記録」 pressed)."""
        self.record_button.setChecked(not self.active)

    def step(self, by: int) -> Optional[ObservedImage]:
        """The next (or previous) image in time (one without its file: the map moves to it when it is out of view)."""
        if self.book is None:
            return None
        before = self.book.current
        image = self.book.step(by)
        if image is not None and before is not None and image.id == before.id:
            # ▶ on the last image (◀ on the first): nothing changes, a form being typed stays.
            return image
        if self._went():
            self.show_image_area(image)
        return self.book.current

    def list_catalog_images(self, images: list, replacing: Sequence[str] = ()) -> Optional[ObservedImage]:
        """
        The catalog's 「ファイルなしで記録」: its images (core.obs.model.ListedImage,
        their ids, times and footprints as the catalog says) listed without
        their files, the first one recorded on; the dock comes forward and
        the map moves to it. The images of `replacing` (those the catalog
        listed before, with 「前の画像を閉じる」) go off the list then, their
        footprints' layers with them; their records stay.
        """
        if self.book is None:
            return None
        entry = self.book.list_images(images, replacing)
        if entry is None:
            return None
        self.list_button.setChecked(False)
        if not self.isVisible():
            self.show()
        self.raise_()
        if self._went():
            self.show_image_area(entry, always=True)
        return entry

    def show_image_area(self, image: Optional[ObservedImage], always: bool = False) -> None:
        """Moves the map to an image without its file that has a footprint (unless already in view, `always` aside)."""
        if image is None or image.file or not image.box:
            return
        west, south, east, north = image.box
        box = QgsRectangle(west, south, east, north)
        transform = from_wgs84(self.canvas.mapSettings().destinationCrs())
        try:
            extent = transform.transformBoundingBox(box)
        except Exception:  # noqa: BLE001 (a footprint the map's CRS cannot show)
            return
        if not always and self.canvas.extent().intersects(extent):
            return
        extent.scale(1.2)
        if extent.isEmpty():
            extent = extent.buffered(max(self.canvas.mapUnitsPerPixel() * 200, 1e-6))
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def go_to(self, image_id: str) -> bool:
        if self.book is None or not self.book.go_to(image_id):
            return False
        return self._went()

    def _image_chosen(self, index: int) -> None:
        if self.book and 0 <= index < len(self.book.images):
            if self.book.current is not None and self.book.images[index].id == self.book.current.id:
                return  # the image recorded on chosen again: nothing changes
            self.book.current = self.book.images[index]
            if self._went():
                self.show_image_area(self.book.current)

    def _toggle_list_form(self, on: bool) -> None:
        self.list_form.setVisible(on)
        if not on:
            return
        image = self.book.current if self.book else None
        listed = image is not None and not image.file
        self.list_id.setText(image.id if listed else "")
        self.list_name.setText(image.name if listed and image.name != image.id else "")
        self.list_time_known.setChecked(bool(listed and image.time is not None))
        self.list_time.setDateTime(QDateTime.fromMSecsSinceEpoch(image.time) if listed and image.time is not None else QDateTime.currentDateTime())
        self.list_box.setChecked(bool(listed and image.box))
        self.unlist_button.setVisible(listed)
        if self.book:
            self.recorded_model.setStringList([i.id for i in self.book.recorded_images()])
        self.list_id.setFocus()

    def _list_from_form(self) -> None:
        if self.book is None:
            return
        image_id = self.list_id.text().strip()
        known = next((i for i in self.book.recorded_images() if i.id == image_id), None)
        time = _ms_of(self.list_time) if self.list_time_known.isChecked() else (known.time if known else None)
        box = self._view_box() if self.list_box.isChecked() else None
        current = self.book.current
        replacing = current.id if current is not None and not current.file else None
        name = self.list_name.text().strip() or (known.name if known else "")
        if self.book.list_image(image_id, name, time, box, replacing=replacing) is not None:
            self.list_button.setChecked(False)
            self._went()

    def _unlist(self) -> None:
        image = self.book.current if self.book else None
        if image is not None and not image.file and self.book.unlist_image(image.id):
            self.list_button.setChecked(False)
            self._went()

    def _show_footprints(self) -> None:
        """The layers of the images without their files as the list is now (nothing done when it did not change)."""
        book = self.book
        image = book.current if book else None
        self.footprints.show(book.listed if book else [], image.id if image is not None else None, book.label if book else "")

    def _footprints_removed(self, image_ids: list[str]) -> None:
        """
        Layers of images without their files removed by hand (tsudo 2026-11-18:
        これを消すことで、観測記録の方からも外れる): those images off the
        list, their records kept, as 「一覧から除外」. Told while the layers
        are being removed: the dock follows once they are gone.
        """
        book = self.book
        if book is None:
            return
        was = book.current
        if not book.unlist_images(image_ids):
            return
        now = book.current
        moved = was is None or now is None or now.id != was.id or now.layer_ids != was.layer_ids
        QTimer.singleShot(0, lambda: self._unlisted(book, moved))

    def unlist_catalog_images(self, image_ids: Sequence[str]) -> int:
        """
        Images the catalog listed without their files taken off the list (its
        「前の画像を閉じる」, the next image opened with its files), their
        footprints' layers with them: how many were on it. Their records stay.
        """
        book = self.book
        if book is None:
            return 0
        was = book.current
        count = book.unlist_images(image_ids)
        if count:
            now = book.current
            self._unlisted(book, was is None or now is None or now.id != was.id or now.layer_ids != was.layer_ids)
        return count

    def _unlisted(self, book: ObservationBook, moved: bool) -> None:
        if self.book is not book:
            return
        if moved:
            self.list_button.setChecked(False)
            self._went()
        else:
            self.render()

    def _set_image_time(self) -> None:
        image = self.book.current if self.book else None
        if image is None:
            return
        t = _ms_of(self.time_input)
        for layer in image_layers(image):
            set_manual_time(layer, t)
        self.do(self.tr("撮像日時の入力"), lambda b: b.set_image_time(t))

    def _set_image_satellite(self) -> None:
        """「衛星不明（入力）」: the 衛星名 typed (or chosen) for the image recorded on; its records and areas without one get it."""
        image = self.book.current if self.book else None
        name = " ".join(self.satellite_input.currentText().split())
        if image is None:
            return
        if not name:
            self._show_status(self.tr("衛星名を入力（例: WV03）"))
            return
        for layer in image_layers(image):
            set_typed_satellite(layer, name)
        self.satellite_input.setEditText("")
        self.do(self.tr("衛星名の入力"), lambda b: b.set_image_satellite(name))

    def _apply_only(self, *_args) -> None:
        """
        「この画像のみ」: only the current image's layers show among the images
        (a record's images in its folder all; backgrounds stay; an image without its file: its footprint's
        layer, the other footprints hidden too); the others come back as they were.
        """
        root = QgsProject.instance().layerTreeRoot()
        footprints = self.footprints.layer_ids()
        # The images' layers only: a background (a GeoTIFF, tiles) stays as it is (操作性点検 1, 2026-11-23).
        images = {layer_id for i in (self.book.images if self.book else ()) for layer_id in i.layer_ids} | set(footprints)
        image = self.book.current if self.book else None
        if self.only.isChecked() and image is not None and self.isVisible():
            if self._shown is None:
                self._shown = {}
            footprint = self.footprints.layer_of(image.id)
            keep = {*image.layer_ids, *((footprint.id(),) if footprint is not None else ())}
            for layer_id in [x for x in self._shown if x not in images]:
                # Made a background meanwhile: as it was.
                node = root.findLayer(layer_id)
                visible = self._shown.pop(layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(visible)
            for layer_id in images:
                node = root.findLayer(layer_id)
                if node is None:
                    continue
                self._shown.setdefault(layer_id, node.itemVisibilityChecked())
                node.setItemVisibilityChecked(layer_id in keep)
        elif self._shown is not None:
            for layer_id, visible in self._shown.items():
                node = root.findLayer(layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(visible)
            self._shown = None

    def _view_box(self) -> list[float]:
        e = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]
