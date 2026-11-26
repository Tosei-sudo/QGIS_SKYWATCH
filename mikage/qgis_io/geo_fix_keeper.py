"""
The images' クイック幾何補正 kept and put back (core/geo_fix.py, geo_fix_io.py):
each image file's correction is kept in the Esri table `imageCorrections`
(shared: anyone who opens the image sees it corrected), else in a file of
this PC's QGIS profile (mikage/image_corrections.json), and put on the image
whenever it is opened (from the catalog or otherwise), with the DEM read for
it first (`dem`; dem_io.py, in a task).

An image is known by its id (the catalog's, else the file name) and the file
(a catalog record's image below its folder, else the file's name), as the
observation records know it (PIXEL_X・PIXEL_Y・IMAGE_FILE).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from typing import Callable, Optional, Sequence

from qgis.core import Qgis, QgsProject, QgsRasterLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..core import edit_stamp
from ..core.geo_fix import GeoFix, delete_fix, fix_from_json, key_of, load_fixes, newest_for, save_fix
from ..core.edit_stamp import now_ms
from . import geo_fix_io
from .tasks import run_task

#: Corrections read from the table are read again after this long.
REREAD_SECONDS = 10 * 60
TASK_DEM = "幾何補正の標高（DEM）を取得"
TASK_READ = "幾何補正を読み込み"
TASK_SAVE = "幾何補正を保存"


def image_key(layer) -> tuple[str, str]:
    """(image id, image file) of a layer, as the records know it."""
    from .obs_images import IMAGE_ID, file_name
    from .pixel_place import layer_file

    image_id = str(layer.customProperty(IMAGE_ID) or "") or file_name(layer)
    return image_id, layer_file(layer)


def local_path() -> str:
    from .qsettings import profile_folder

    folder = profile_folder()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "image_corrections.json")


class LocalFixes:
    """The corrections kept on this PC (no `imageCorrections`): one JSON file, one entry per image file."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or local_path()

    def _read(self) -> list[GeoFix]:
        try:
            with open(self.path, encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            return []
        items = value.get("fixes") if isinstance(value, dict) else None
        out = []
        for item in items or []:
            fix = fix_from_json(json.dumps(item))
            if fix is not None:
                out.append(fix)
        return out

    def _write(self, fixes: list[GeoFix]) -> None:
        part = self.path + ".part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump({"fixes": [json.loads(x.to_json()) for x in fixes]}, f, ensure_ascii=False, indent=1)
        os.replace(part, self.path)

    def load(self, image_ids: Sequence[str]) -> list[GeoFix]:
        ids = set(image_ids)
        return [x for x in self._read() if x.image_id in ids]

    def save(self, fix: GeoFix) -> GeoFix:
        fixes = [x for x in self._read() if x.key != fix.key]
        self._write(fixes + [fix])
        return fix

    def delete(self, fix: GeoFix) -> int:
        fixes = self._read()
        kept = [x for x in fixes if x.key != fix.key]
        self._write(kept)
        return len(fixes) - len(kept)


class FixKeeper(QObject):
    """Reads, saves and puts back the images' corrections (main thread; the network and the DEM in tasks)."""

    #: A layer's correction was put on, changed or taken off (its id).
    changed = pyqtSignal(str)
    #: The corrections read for images (their ids).
    read = pyqtSignal(list)

    def __init__(self, context, iface=None, parent: Optional[QObject] = None, local: Optional[LocalFixes] = None):
        super().__init__(parent)
        self.context = context
        self.iface = iface
        self.local = local or LocalFixes()
        #: Corrections read, by image id: (when, [GeoFix]).
        self._known: dict[str, tuple[float, list]] = {}
        self._tasks: list = []
        #: Layers being prepared (their DEM read), not started again meanwhile.
        self._preparing: set = set()
        context.configChanged.connect(self._config_changed)

    # ---- settings ----

    @property
    def source(self):
        return getattr(self.context.config, "image_corrections", None)

    @property
    def shared(self) -> bool:
        return self.source is not None

    @property
    def dem(self):
        return getattr(self.context.config, "dem", None)

    def where(self) -> str:
        """Where the corrections are kept, in words."""
        return "Esri の表（共有）" if self.shared else "この PC"

    def _config_changed(self) -> None:
        self._known = {}

    def _start(self, description: str, work, done, failed) -> None:
        task = run_task(description, work, done, failed)
        self._tasks.append(task)
        for signal in (task.taskCompleted, task.taskTerminated):
            signal.connect(lambda task=task: self._tasks.remove(task) if task in self._tasks else None)

    # ---- reading ----

    def known(self, image_id: str, image_file: str) -> Optional[GeoFix]:
        """The correction of an image file, of those read (None: none, or not read)."""
        entry = self._known.get(image_id)
        return newest_for(entry[1], image_id, image_file) if entry else None

    def is_read(self, image_id: str) -> bool:
        entry = self._known.get(image_id)
        return entry is not None and time.time() - entry[0] < REREAD_SECONDS

    def load(self, image_ids: Sequence[str], done: Optional[Callable[[list], None]] = None,
             failed: Optional[Callable[[str], None]] = None) -> None:
        """Reads the corrections of images (a task for the table, at once from this PC's file)."""
        ids = sorted({i for i in image_ids if i})
        if not ids:
            if done:
                done([])
            return
        if not self.shared:
            fixes = self.local.load(ids)
            self._remember(ids, fixes)
            if done:
                done(fixes)
            return
        source, context = self.source, self.context

        def work(task):
            return load_fixes(context.esri(), source, ids, context.token_for(source.url, source.token), task.isCanceled)

        def finished(fixes) -> None:
            self._remember(ids, fixes)
            if done:
                done(fixes)

        self._start(TASK_READ, work, finished, failed or self._say_failed)

    def _remember(self, ids: Sequence[str], fixes: list) -> None:
        now = time.time()
        for i in ids:
            self._known[i] = (now, [x for x in fixes if x.image_id == i])
        self.read.emit(list(ids))

    # ---- saving ----

    def save(self, fix: GeoFix, done: Optional[Callable[[GeoFix], None]] = None, failed: Optional[Callable[[str], None]] = None) -> None:
        """Keeps an image file's correction (who and when stamped), shared when the table is set."""
        fix = replace(fix, by=edit_stamp.user_name(), at=now_ms())
        if not self.shared:
            try:
                saved = self.local.save(fix)
            except OSError as e:
                (failed or self._say_failed)(str(e))
                return
            self._saved(saved, done)
            return
        source, context = self.source, self.context

        def work(_task):
            return save_fix(context.esri(), source, fix, context.token_for(source.url, source.token))

        self._start(TASK_SAVE, work, lambda saved: self._saved(saved, done), failed or self._say_failed)

    def _saved(self, fix: GeoFix, done) -> None:
        when, fixes = self._known.get(fix.image_id, (time.time(), []))
        self._known[fix.image_id] = (when, [x for x in fixes if x.key != fix.key] + [fix])
        if done:
            done(fix)

    def delete(self, fix: GeoFix, done: Optional[Callable[[int], None]] = None, failed: Optional[Callable[[str], None]] = None) -> None:
        """Deletes an image file's kept correction (for everyone, when shared)."""
        def forget(n: int) -> None:
            when, fixes = self._known.get(fix.image_id, (time.time(), []))
            self._known[fix.image_id] = (when, [x for x in fixes if x.key != fix.key])
            if done:
                done(n)

        if not self.shared:
            try:
                forget(self.local.delete(fix))
            except OSError as e:
                (failed or self._say_failed)(str(e))
            return
        source, context = self.source, self.context

        def work(_task):
            return delete_fix(context.esri(), source, fix, context.token_for(source.url, source.token))

        self._start(TASK_SAVE, work, forget, failed or self._say_failed)

    # ---- putting a correction on a layer ----

    def dem_box(self, layer) -> Optional[tuple]:
        """The area of the DEM a layer needs (its file's footprint with room for the terrain's moves), None when unknown."""
        from . import shift_io

        try:
            base = geo_fix_io.open_base(shift_io.original_source(layer).split("|")[0])
        except geo_fix_io.GeoFixError:
            return None
        box = base.footprint()
        if box is None:
            return None
        west, south, east, north = box
        mx, my = (east - west) * 0.2 + 0.03, (north - south) * 0.2 + 0.03
        return west - mx, south - my, east + mx, north + my

    def fetch_dem(self, box: tuple, cancelled: Callable[[], bool] = lambda: False, progress=lambda _p: None) -> str:
        """The DEM's file over an area (in a task): '' without `dem`."""
        from . import settings
        from .dem_io import fetch_dem
        from .network import qgis_transport

        dem = self.dem
        if dem is None or not dem.configured:
            return ""
        context = self.context
        token = context.token_for(dem.url, dem.token) if dem.url else None
        client = context.esri() if dem.url else None
        post = qgis_transport(settings.auth_for)
        return fetch_dem(dem, box, lambda url, params: client.ask(url, params, token), post, token, cancelled=cancelled,
                         progress=progress)

    def prepare(self, layer: QgsRasterLayer, fix: GeoFix, done: Callable[[object], None],
                failed: Optional[Callable[[str], None]] = None) -> None:
        """Reads the DEM a correction needs (a task, when the terrain is corrected), then puts it on the layer (main thread)."""
        layer_id = layer.id()
        source = None
        from . import shift_io

        source = shift_io.original_source(layer).split("|")[0]
        need_dem = fix.terrain and self.dem is not None and self.dem.configured
        geoid = self.dem.geoid_height if self.dem is not None else 0.0

        def put(dem_path: str) -> None:
            self._preparing.discard(layer_id)
            target = QgsProject.instance().mapLayer(layer_id)
            if target is None or sip.isdeleted(target):
                return
            try:
                report = geo_fix_io.apply(target, fix, dem_path, geoid)
            except (geo_fix_io.GeoFixError, OSError, RuntimeError, ValueError) as error:
                (failed or self._say_failed)(str(error))
                return
            self.changed.emit(layer_id)
            done(report)

        if not need_dem:
            put("")
            return
        self._preparing.add(layer_id)

        def work(task):
            base = geo_fix_io.open_base(source)
            box = base.footprint()
            if box is None:
                raise geo_fix_io.GeoFixError("画像の範囲が求められない", code="E508")
            west, south, east, north = box
            mx, my = (east - west) * 0.2 + 0.03, (north - south) * 0.2 + 0.03
            return self.fetch_dem((west - mx, south - my, east + mx, north + my), task.isCanceled, lambda p: task.progress(p * 100))

        def gone(message: str) -> None:
            self._preparing.discard(layer_id)
            (failed or self._say_failed)(message)

        self._start(TASK_DEM, work, put, gone)

    def take_off(self, layer: QgsRasterLayer) -> None:
        """The layer shown as its file places it again (the kept correction stays)."""
        geo_fix_io.remove(layer)
        self.changed.emit(layer.id())

    # ---- opened images ----

    def layers_added(self, layers) -> None:
        """Images opened: those with a kept correction get it (read first; the DEM in a task)."""
        from .shift_io import can_shift

        rasters = [layer for layer in layers if isinstance(layer, QgsRasterLayer) and layer.providerType() == "gdal"]
        # A project's corrected layer whose VRT went (the profile cleaned, another PC): its file again, then corrected again.
        for layer in rasters:
            if geo_fix_io.is_fixed(layer) and not os.path.exists(layer.source().split("|")[0]):
                fix = geo_fix_io.fix_of(layer)
                try:
                    geo_fix_io.remove(layer)
                except Exception:  # noqa: BLE001 (the file gone too: the layer stays as it is)
                    continue
                if fix is not None and can_shift(layer):
                    self.prepare(layer, fix, lambda _r: None)
        fresh = [layer for layer in rasters if can_shift(layer) and not geo_fix_io.is_fixed(layer) and layer.id() not in self._preparing]
        if not fresh:
            return
        ids = {layer.id(): image_key(layer) for layer in fresh}

        def done(_fixes) -> None:
            for layer_id, (image_id, image_file) in ids.items():
                layer = QgsProject.instance().mapLayer(layer_id)
                fix = self.known(image_id, image_file)
                if layer is None or fix is None or geo_fix_io.is_fixed(layer) or layer_id in self._preparing:
                    continue
                name = layer.name()
                self.prepare(layer, fix, lambda report, name=name, fix=fix: self._say(
                    f"{name}: 保存された幾何補正を適用（{fix.summary()}、{fix.by or '不明'}）"))

        self.load(sorted({v[0] for v in ids.values()}), done, lambda message: self._say_failed(message))

    # ---- telling ----

    def _say(self, text: str) -> None:
        if self.iface is not None:
            self.iface.messageBar().pushMessage("クイック幾何補正", text, Qgis.MessageLevel.Info, 8)

    def _say_failed(self, message: str) -> None:
        if self.iface is not None:
            self.iface.messageBar().pushMessage("クイック幾何補正", message, Qgis.MessageLevel.Warning, 10)

    def unload(self) -> None:
        for task in list(self._tasks):
            try:
                if not sip.isdeleted(task):
                    task.cancel()
            except RuntimeError:
                pass
        self._tasks = []
        try:
            self.context.configChanged.disconnect(self._config_changed)
        except (TypeError, RuntimeError):
            pass


def later(slot) -> None:
    QTimer.singleShot(0, slot)
