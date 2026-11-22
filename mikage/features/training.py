"""
練習モード (2026-11-22, tsudo 「トレーニングモードを用意して、チュートリアル機能を付与したい」):
the plugin on practice data, apart from the production. 「練習モードを始める」
starts the practice server inside QGIS (127.0.0.1 only, core/training) on the
sample of Tokyo Bay (or the organisation's practice settings file,
`training.config`), switches the settings file and the local files to the
practice's (qgis_io/training_io.py), opens a new project with the practice's
background map, marks the screens (ui/training_mode.py) and pauses the audit
log; 「練習モードを終える」 (or QGIS closing) puts everything back and opens
the project of before again. 「練習データを最初に戻す…」 makes the sample again.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox, QProgressDialog

from ..core.config import load_jsonc
from ..core.training.api import AREA, DEFAULT_TRAINING, masters, resolve_config
from ..qgis_io import settings, training_io
from ..qgis_io.tasks import run_task
from ..ui.training_mode import StartDialog, TitleMarks, TrainingBand, status_mark
from .base import Feature, disconnect, tr

#: The menu entries (MENU_GROUPS 「練習モード」), hidden when the settings file's `training` is false.
START, RESET, END = "練習モードを始める", "練習データを最初に戻す…", "練習モードを終える"
ENTRIES = (START, RESET, END)
#: What cannot be used while practising (it would reach outside the practice): disabled, with why.
NOT_IN_PRACTICE = ("設定…", "サーバ処理…", "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…", "移動体（AIS・ADS-B）")
NOTE = "（練習モードでは使えません）"
#: While the taxonomy is the production's (read only: its table is not edited while practising).
TAXONOMY_EDIT, TAXONOMY_NOTE = "分類体系の編集", "（練習モードでは本番の分類体系を読むだけ）"
#: The practice's background map (a layer of the practice's project).
BASEMAP_NAME = "練習用の背景地図"
BAND_TEXT = "練習用のデータで動いています。本番のサーバーには書き込みません。"


class Training(Feature):
    COMMANDS = ("start_training", "end_training", "reset_training")

    def __init__(self, host):
        super().__init__(host)
        #: The production settings file's `training` (read while not practising: the practice's own has none).
        self.settings = DEFAULT_TRAINING
        self.band: Optional[TrainingBand] = None
        self.mark = None
        self.marks: Optional[TitleMarks] = None
        #: The project open before practising (opened again after; "" : a new one).
        self.project_before = ""
        self.progress: Optional[QProgressDialog] = None
        self._busy = False
        self._actions: dict = {}

    @property
    def practising(self) -> bool:
        return training_io.active() is not None

    # ---- the hooks ---------------------------------------------------------------------------------------

    def start(self) -> None:
        # QGIS stopped while practising: what was kept aside of the profile comes back (担当 among it).
        if training_io.recover():
            if self.host.role_keeper is not None:
                self.host.role_keeper.use_stored()
            self.iface.messageBar().pushInfo(tr("練習モード"), tr("前回は練習モードのまま QGIS が終わったため、担当などを練習の前に戻しました"))
        self.context.configChanged.connect(self._config_read)

    def add_actions(self) -> None:
        self._actions = {
            START: self.entry("training", START, "start_training", name="SatObsTrainingStart"),
            RESET: self.entry("training_reset", RESET, "reset_training", name="SatObsTrainingReset"),
            END: self.entry("training_end", END, "end_training", name="SatObsTrainingEnd"),
        }
        self._entries_state()

    def unload(self) -> None:
        # QGIS closing (or the plugin turned off) while practising: back to the production quietly (the map left as it is).
        disconnect(self.context.configChanged, self._config_read)
        if self.practising:
            training_io.end()
            if self.host.audit is not None:
                self.host.audit.practise(False, {"how": "unload"})
            self._screens(False)
        if self.progress is not None:
            self.progress.close()
            self.progress = None

    # ---- the settings file's `training` -----------------------------------------------------------------

    def _config_read(self) -> None:
        if self.context.loading or self.practising:
            return
        self.settings = self.context.config.training
        hidden = set() if self.settings.enabled else {tr(t) for t in ENTRIES}
        others = self.host.hidden_entries - {tr(t) for t in ENTRIES}
        if others | hidden != self.host.hidden_entries:
            self.host.hidden_entries = others | hidden
            self.host.apply_roles()

    def _entries_state(self) -> None:
        on = self.practising
        for text, action in self._actions.items():
            action.setEnabled(not self._busy and (on if text == END else not on))
        reset = self._actions.get(RESET)
        if reset is not None:
            reset.setEnabled(not self._busy and self.settings.builtin)

    # ---- starting ----------------------------------------------------------------------------------------

    def start_training(self) -> bool:
        """「練習モードを始める」: asked first; False when it did not start (asked to stop, or already on)."""
        if self.practising or self._busy:
            return False
        if not self.settings.enabled:
            self.iface.messageBar().pushInfo(tr("練習モード"), tr("設定ファイルの training が false のため、練習モードは使えません"))
            return False
        builtin = self.settings.builtin
        files = training_io.files()
        ask = StartDialog(self.window(), builtin, files.created() if files.ready() else None,
                          from_production=masters.names_text(self.settings.from_production) if builtin else "")
        if ask.exec() != StartDialog.DialogCode.Accepted:
            return False
        if not self._project_saved():
            return False
        self.project_before = QgsProject.instance().fileName()
        if builtin and (not files.ready() or ask.renew.isChecked()):
            self._prepare(files, reset=files.ready(), then=lambda: self._switch_on(files, builtin))
        else:
            self._switch_on(files, builtin)
        return True

    def _project_saved(self) -> bool:
        """The project of before saved if it has changes and the person wants to (False: asked to stop)."""
        project = QgsProject.instance()
        if not project.isDirty():
            return True
        answer = QMessageBox.question(
            self.window(), tr("練習モードを始める"),
            tr("いまのプロジェクトに保存していない変更があります。練習の前に保存しますか？"),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self.iface.actionSaveProject().trigger()
            if project.isDirty():  # (the file dialog cancelled)
                return False
        return True

    def _prepare(self, files, reset: bool, then) -> None:
        """The sample made in a task (its images drawn: a few seconds), with a window saying so; `then` once made."""
        self._busy = True
        self._entries_state()
        self.progress = QProgressDialog(tr("練習用の画像と背景地図を作っています…"), "", 0, 0, self.window())
        self.progress.setWindowTitle(tr("練習モード"))
        self.progress.setCancelButton(None)
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.show()

        def work(task):
            training_io.prepare(files, reset=reset, cancelled=task.isCanceled)
            return True

        def finished(ok: bool, message: str = "") -> None:
            self._busy = False
            if self.progress is not None:
                self.progress.close()
                self.progress = None
            self._entries_state()
            if ok:
                then()
            else:
                self.iface.messageBar().pushWarning(tr("練習モード"), tr("練習データを作れませんでした: {m}").format(m=message))

        run_task(tr("練習データの作成"), work, lambda _ok: finished(True), lambda message: finished(False, message))

    def _production(self) -> Optional[training_io.Production]:
        """The production's settings file as read now, with the masters to take from it (None: none to take, or not readable)."""
        names = self.settings.from_production
        source = settings.production_config_source()[0]
        if not names or not source:
            return None
        value = None
        try:
            if self.context.source == source and self.context.config_text is not None:
                value = load_jsonc(self.context.config_text)  # (read from a URL, with its tokens)
            elif re.match(r"^https?://", source, re.IGNORECASE):
                cached = settings.cached_config(source)
                value = cached[0] if cached is not None else None
            else:
                with open(source, encoding="utf-8-sig") as f:
                    value = load_jsonc(f.read())
        except (OSError, ValueError):
            value = None
        return training_io.Production(value, source, tuple(names)) if isinstance(value, dict) else None

    def _switch_on(self, files, builtin: bool) -> None:
        production = self._production() if builtin else None
        try:
            server, text, taken = training_io.start_server(files, production) if builtin else (None, None, ())
        except OSError as error:
            self.iface.messageBar().pushWarning(tr("練習モード"), str(error))
            return
        source = files.config if builtin else resolve_config(self.settings.config, settings.production_config_source()[0])
        # In the production's audit log, then nothing more until it ends.
        if self.host.audit is not None:
            self.host.audit.practise(True, {"builtin": builtin, "config": "" if builtin else source})
        training_io.begin(training_io.TrainingSession(files, source, builtin, server, config_text=text, production=production,
                                                      from_production=taken))
        self.context.switch_files()
        self.context.reload()
        self._forget_lists()
        # A new project: no production layer left on the map (the one of before saved or let go already).
        QgsProject.instance().setDirty(False)
        self.iface.newProject(False)
        if builtin:
            self._add_basemap(files)
        self._screens(True)
        self._entries_state()
        read = tr("{m}は本番から読んでいます（読むだけ）。").format(m=masters.names_text(taken)) if taken else ""
        self.iface.messageBar().pushMessage(tr("練習モード"), tr("練習モードを始めました。") + read + tr("終えるときは地図の上の「練習モードを終える」"),
                                            Qgis.MessageLevel.Success, 8)

    def _add_basemap(self, files) -> None:
        if os.path.exists(files.basemap):
            layer = QgsRasterLayer(files.basemap, tr(BASEMAP_NAME))
            if layer.isValid():
                project = QgsProject.instance()
                project.addMapLayer(layer, False)
                project.layerTreeRoot().addLayer(layer)  # (at the bottom)
        canvas = self.iface.mapCanvas()
        west, south, east, north = AREA
        box = QgsRectangle(west - 0.03, south - 0.02, east + 0.03, north + 0.02)
        crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs.isValid() and crs != wgs84:
            box = QgsCoordinateTransform(wgs84, crs, QgsProject.instance()).transformBoundingBox(box)
        canvas.setExtent(box)
        canvas.refresh()
        QgsProject.instance().setDirty(False)

    # ---- ending ------------------------------------------------------------------------------------------

    def end_training(self) -> bool:
        """「練習モードを終える」: back to the production and its project; False when not practising."""
        if not self.practising or self._busy:
            return False
        training_io.end()
        self._screens(False)
        if self.host.role_keeper is not None:
            self.host.role_keeper.use_stored()  # (the roles before practising: the profile has them back)
        self.context.switch_files()
        self.context.reload()
        self._forget_lists()
        if self.host.audit is not None:
            self.host.audit.practise(False)
        self._entries_state()
        # The practice's project let go (nothing to keep), the one of before opened again once the settings file is read.
        QgsProject.instance().setDirty(False)
        if self.context.loading:
            self.context.configChanged.connect(self._open_project_before)
        else:
            self._open_project_before()
        self.iface.messageBar().pushMessage(tr("練習モード"), tr("練習モードを終えました（練習データは次の練習まで残ります）"),
                                            Qgis.MessageLevel.Info, 6)
        return True

    def _open_project_before(self) -> None:
        disconnect(self.context.configChanged, self._open_project_before)
        before, self.project_before = self.project_before, ""
        QgsProject.instance().setDirty(False)
        if before and os.path.exists(before):
            self.iface.addProject(before)
        else:
            self.iface.newProject(False)

    # ---- 練習データを最初に戻す ------------------------------------------------------------------------

    def reset_training(self) -> bool:
        """「練習データを最初に戻す…」: the sample made again (asked first); while practising, practised on at once."""
        if self._busy or not self.settings.builtin:
            return False
        answer = QMessageBox.question(
            self.window(), tr("練習データを最初に戻す"),
            tr("練習データを最初の見本に戻します。練習で記録したもの・依頼・計画ファイルなどは消えます。よろしいですか？"),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return False
        session = training_io.active()
        files = training_io.files()
        if session is None:
            self._prepare(files, reset=True, then=lambda: self.iface.messageBar().pushSuccess(tr("練習モード"), tr("練習データを最初に戻しました")))
            return True
        # Practising: the map and the files let go, the data made again, the server started again on it.
        QgsProject.instance().setDirty(False)
        self.iface.newProject(False)
        if session.server is not None:
            session.server.stop()
        self.context.release_files()

        def again() -> None:
            try:
                session.server, session.config_text, session.from_production = training_io.start_server(files, session.production)
            except OSError as error:
                self.iface.messageBar().pushWarning(tr("練習モード"), str(error))
                return
            self.context.switch_files()
            self.context.reload()
            self._forget_lists()
            self._add_basemap(files)
            self.iface.messageBar().pushSuccess(tr("練習モード"), tr("練習データを最初に戻しました"))

        self._prepare(files, reset=True, then=again)
        return True

    def _forget_lists(self) -> None:
        """The image catalog's list emptied: its images are of the server left (not there any more after practising)."""
        catalog = getattr(getattr(self.host, "api", None), "image_catalog", None)
        if catalog is not None and hasattr(catalog, "forget_list"):
            catalog.forget_list()

    # ---- the marks on the screens ------------------------------------------------------------------------

    def _screens(self, on: bool) -> None:
        """The band on the map, the status bar's mark, the titles' marks and the entries not usable while practising."""
        window = self.window()
        if on:
            canvas = self.iface.mapCanvas()
            if self.band is None:
                self.band = TrainingBand(canvas, [(tr(RESET), self.reset_training), (tr(END), self.end_training)], tr(BAND_TEXT),
                                         cover=self.iface.messageBar())
            if self.mark is None:
                self.mark = status_mark()
                window.statusBar().addPermanentWidget(self.mark)
            if self.marks is None:
                self.marks = TitleMarks(window, list(self.docks.values()))
            notes = {tr(t): tr(NOTE) for t in NOT_IN_PRACTICE}
            session = training_io.active()
            if session is not None and "taxonomy" in session.from_production:
                notes[tr(TAXONOMY_EDIT)] = tr(TAXONOMY_NOTE)
            self.host.block_entries(notes)
            for name, dock in self.docks.items():
                if self.host.dock_entry_text(name) in notes:
                    dock.hide()
            QgsProject.instance().isDirtyChanged.connect(self._keep_clean)
            return
        disconnect(QgsProject.instance().isDirtyChanged, self._keep_clean)
        self.host.block_entries({})
        if self.marks is not None:
            self.marks.remove()
            self.marks = None
        if self.band is not None:
            self.band.remove()
            self.band = None
        if self.mark is not None:
            try:
                window.statusBar().removeWidget(self.mark)
                self.mark.deleteLater()
            except RuntimeError:
                pass
            self.mark = None

    def _keep_clean(self, dirty: bool) -> None:
        """The practice's project never asks to be saved (when it ends, or QGIS closes while practising)."""
        if dirty and self.practising:
            QgsProject.instance().setDirty(False)
