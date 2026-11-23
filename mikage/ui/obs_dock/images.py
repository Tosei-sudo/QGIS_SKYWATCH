"""
観測記録ドックの画像 (ui/obs_dock, docs/architecture.md 段階7): the images recorded on: their list, the one chosen,
its time and satellite, the footprints and layers. A part of ObservationDock (ui/observation_dock.py):
its methods as they were, on the dock's widgets and state.
"""
from __future__ import annotations

from typing import Optional

from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import QDateTime, QTimer

from ...core.obs.api import ObservationBook, ObservedImage
from ...core.obs.api import SATELLITE_ORIGINS
from ...qgis_io.geometry import from_wgs84, to_wgs84
from ...qgis_io.listed_layers import listed_image_of
from ...qgis_io.obs_images import SatelliteLookup, image_layers, open_images, set_manual_time, set_typed_satellite
from ...qgis_io.tasks import run_task
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

    def _layer_chosen(self, layer) -> None:
        if self._following or self.book is None:
            return
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

    def _went(self) -> None:
        """
        Another image: the form closes, the layer is chosen, 「この画像のみ」
        follows; 「クリック記録」 goes on once the image is taken (分担).
        """
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
        self._apply_only()
        self.render()
        self.refresh()

    def toggle_recording(self) -> None:
        """Starts recording, or ends it (「記録」 pressed)."""
        self.record_button.setChecked(not self.active)

    def step(self, by: int) -> Optional[ObservedImage]:
        """The next (or previous) image in time (one without its file: the map moves to it when it is out of view)."""
        if self.book is None:
            return None
        image = self.book.step(by)
        self._went()
        self.show_image_area(image)
        return image

    def list_catalog_images(self, images: list) -> Optional[ObservedImage]:
        """
        The catalog's 「ファイルなしで記録」: its images (core.obs.model.ListedImage,
        their ids, times and footprints as the catalog says) listed without
        their files, the first one recorded on; the dock comes forward and
        the map moves to it.
        """
        if self.book is None:
            return None
        entry = self.book.list_images(images)
        if entry is None:
            return None
        self.list_button.setChecked(False)
        if not self.isVisible():
            self.show()
        self.raise_()
        self._went()
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
        self._went()
        return True

    def _image_chosen(self, index: int) -> None:
        if self.book and 0 <= index < len(self.book.images):
            self.book.current = self.book.images[index]
            self._went()
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
            self._show_status(self.tr("衛星名を入れる（例: WV03）"))
            return
        for layer in image_layers(image):
            set_typed_satellite(layer, name)
        self.satellite_input.setEditText("")
        self.do(self.tr("衛星名の入力"), lambda b: b.set_image_satellite(name))

    def _apply_only(self, *_args) -> None:
        """
        「この画像のみ」: only the current image's layers show (a record's
        images in its folder all; an image without its file: its footprint's
        layer, the other footprints hidden too); the others come back as they were.
        """
        root = QgsProject.instance().layerTreeRoot()
        footprints = self.footprints.layer_ids()
        rasters = [layer for layer in QgsProject.instance().mapLayers().values()
                   if isinstance(layer, QgsRasterLayer) or layer.id() in footprints]
        image = self.book.current if self.book else None
        if self.only.isChecked() and image is not None and self.isVisible():
            if self._shown is None:
                self._shown = {}
            footprint = self.footprints.layer_of(image.id)
            keep = {*image.layer_ids, *((footprint.id(),) if footprint is not None else ())}
            for layer in rasters:
                node = root.findLayer(layer.id())
                if node is None:
                    continue
                self._shown.setdefault(layer.id(), node.itemVisibilityChecked())
                node.setItemVisibilityChecked(layer.id() in keep)
        elif self._shown is not None:
            for layer_id, visible in self._shown.items():
                node = root.findLayer(layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(visible)
            self._shown = None

    def _view_box(self) -> list[float]:
        e = to_wgs84(self.canvas.mapSettings().destinationCrs()).transformBoundingBox(self.canvas.extent())
        return [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]
