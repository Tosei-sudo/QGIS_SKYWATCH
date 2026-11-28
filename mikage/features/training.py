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
「チュートリアル…」 (練習モードの段階 2) walks through the help's 画面つきの手順書
one operation at a time on the practice data (ui/tutorial_guide.py and
ui/tutorial_courses.py; 練習モード started first when not practising).
「判読の練習…」 (段階 3) offers exercises in reading images — find, tell apart,
measure, find what changed — answered with the observation dock's usual
operations, scored against an interpreter's answers and reviewed
(ui/exercise_run.py; the sample's exercises on the built-in sample). The
instructors (担当「判読練習の指導者」) make the organisation's in the production
MIKAGE — 「判読の練習の問題を作る…」 writes their tables (ui/exercise_authoring.py)
— and read everyone's results in 「判読の練習の成績…」 (ui/exercise_grades.py).

MIKAGE 練習版 (core/edition.py, `scripts/build_zip.py --training-only`):
練習モード starts by itself once QGIS has started (no question, the sample
made first the first time) and never ends: 「練習モードを始める」「練習モード
を終える」 and the instructors' entries are not there. The screens never get
the production's settings file (qgis_io/settings.effective_config_source):
of the settings file shipped in the zip (none: the built-in sample only) this
feature reads what 練習モード takes of a production (`training`, the masters,
the practice catalogs, the organisation's exercises). The project open is
kept (there is no production layer to let go of).
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRasterLayer, QgsRectangle
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QMessageBox, QProgressDialog, QToolBar

from ..core import edit_stamp, edition, roles
from ..core.catalog.api import records_by_ids
from ..core.config import load_jsonc, parse_config, read_config_file
from ..core.training.api import (
    AREA, Column, DEFAULT_TRAINING, delete_draft, drafts_of, exam_taken, kept, mark_sent, masters, merged_results, read_all_results, read_drafts,
    read_exercises, read_progress, read_results, read_results_of, resolve_config, sample_exercises, save_draft, send_results, set_active,
    unsent, write_progress,
)
from ..qgis_io import settings, training_io
from ..qgis_io.catalog_io import local_path
from ..qgis_io.tasks import error_text, run_task
from ..ui.exercise_authoring import ExerciseAuthoring
from ..ui.exercise_grades import GradesDialog
from ..ui.exercise_run import ExerciseRun, result_line
from ..ui.exercise_screens import ExerciseList, MyResults
from ..ui.training_mode import StartDialog, TitleMarks, TrainingBand, status_mark
from ..ui.tutorial_courses import courses
from ..ui.tutorial_guide import CourseList, Tutorial
from .base import Feature, disconnect, tr

#: The menu entries (MENU_GROUPS 「練習モード」), hidden when the settings file's `training` is false.
START, TUTORIAL, EXERCISES, RESET, END = ("練習モードを始める", "チュートリアル…", "判読の練習…", "練習データを最初に戻す…",
                                          "練習モードを終える")
#: The instructors' (担当「判読練習の指導者」), shown when the settings file has the organisation's tables.
AUTHOR, GRADES = "判読の練習の問題を作る…", "判読の練習の成績…"
ENTRIES = (START, TUTORIAL, EXERCISES, RESET, END, AUTHOR, GRADES)
#: Not in MIKAGE 練習版: it is always practising, and the instructors work in the production MIKAGE.
NOT_IN_TRAINING_EDITION = (START, END, AUTHOR, GRADES)
#: The band's buttons for the courses and the exercises.
TUTORIAL_BUTTON, EXERCISES_BUTTON = "チュートリアル", "判読の練習"
#: The role the exercises are solved in (its observation dock).
EXERCISE_ROLE = "OBSERVER"
#: What cannot be used while practising (it would reach outside the practice): disabled, with why.
NOT_IN_PRACTICE = ("設定…", "サーバ処理…", "ArcGIS Web マップを開く…", "ArcGIS Web マップとして保存…", "移動体（AIS・ADS-B）")
NOTE = "（練習モードでは使えない）"
#: While the taxonomy is the production's (read only: its table is not edited while practising).
TAXONOMY_EDIT, TAXONOMY_NOTE = "分類体系の編集", "（練習モードでは本番の分類体系を読むだけ）"
#: 「分類体系の取り込み…」 (the table import) is changing it too.
TAXONOMY_IMPORT = "分類体系の取り込み…"
#: The practice's background map (a layer of the practice's project).
BASEMAP_NAME = "練習用の背景地図"
BAND_TEXT = "練習用のデータで動作中。本番には書き込まない"
EDITION_BAND_TEXT = "MIKAGE 練習版。練習用のデータだけで動作"


class TutorialPlace:
    """What チュートリアル's courses reach of the plugin (ui/tutorial_courses.py): its API, buttons, help and the sample."""

    def __init__(self, feature: "Training"):
        self.feature = feature
        self.host = feature.host
        self.api = feature.host.api
        self.iface = feature.iface
        self.canvas = feature.iface.mapCanvas()
        session = training_io.active()
        self.builtin = session is not None and session.builtin
        files = session.files if session is not None else training_io.files()
        #: The practice's folder of the files it writes.
        self.outputs = files.outputs
        self._images = files.sample_images
        self._ships: Optional[dict] = None

    @property
    def band(self):
        return self.feature.band

    @property
    def toolbar(self):
        return self.host.toolbar

    def button(self, text: str):
        """The button of a menu entry on a toolbar shown (MIKAGE's, or one of the person's own); None when none shows."""
        action = self.host.entry_action(text)
        if action is None:
            return None
        bars = [self.host.toolbar, *self.feature.window().findChildren(QToolBar)]
        for bar in bars:
            widget = bar.widgetForAction(action) if bar is not None and bar.isVisible() else None
            if widget is not None and widget.isVisible():
                return widget
        return None

    def trigger(self, text: str) -> bool:
        return self.host.run_menu_entry(text)

    def help_window(self):
        return getattr(self.host, "help_dialog", None)

    def ships(self, image_id: str) -> list:
        """The things the sample drew in an image (longitude, latitude…); [] for another image."""
        if self._ships is None:
            try:
                with open(self._images, encoding="utf-8") as f:
                    self._ships = {p["id"]: p.get("ships") or [] for p in json.load(f) if isinstance(p, dict) and "id" in p}
            except (OSError, ValueError, TypeError):
                self._ships = {}
        return list(self._ships.get(image_id, []))

    def to_sample(self) -> None:
        """The map on the sample's area (Yokosuka)."""
        self.feature.show_area()


class ExerciseTablesIO:
    """問題づくり's tables (the settings file's training.exercises) for ui/exercise_authoring.py: each read and write in a task."""

    def __init__(self, feature: "Training"):
        self.feature = feature
        self.tables = feature.settings.exercises
        self.scoring = feature.settings.scoring
        self.roles = {r.code: r.name for r in roles.ROLES}

    def _run(self, title: str, work, then, failed) -> None:
        client, token = self.feature.context.esri(), None
        run_task(tr(title), lambda _task: work(client, token), then, failed)

    def read_drafts(self, then, failed) -> None:
        self._run("判読の練習: 問題の取得", lambda c, t: read_drafts(c, self.tables, t), then, failed)

    def save_draft(self, draft, then, failed) -> None:
        self._run("判読の練習: 問題の保存", lambda c, t: save_draft(c, self.tables, draft, t), then, failed)

    def set_active(self, draft, active: bool, then, failed) -> None:
        self._run("判読の練習: 問題の保存", lambda c, t: set_active(c, self.tables, draft, active, t), then, failed)

    def delete_draft(self, draft, then, failed) -> None:
        self._run("判読の練習: 問題の削除", lambda c, t: delete_draft(c, self.tables, draft, t), then, failed)

    def try_out(self, exercise) -> None:
        self.feature.try_exercise(exercise)


class Training(Feature):
    COMMANDS = ("start_training", "end_training", "reset_training", "open_tutorial", "open_exercises", "open_exercise_authoring",
                "open_exercise_grades")

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
        #: The course running (チュートリアル), None when none.
        self.tutorial: Optional[Tutorial] = None
        #: Whether the courses are offered once 練習モード has started (asked for by 「チュートリアル…」 or the question).
        self._tutorial_after_start = False
        #: 判読の練習: the exercise being solved (None when none), and whether the list is offered once 練習モード has started.
        self.exercise_run: Optional[ExerciseRun] = None
        self._exercises_after_start = False
        #: The organisation's exercises (training.exercises) as last read, the person's tries in its results table,
        #: and why they could not be read ("" : they could); whether they are being read now.
        self.org_exercises: list = []
        self.org_results: list = []
        self.org_problem = ""
        self._reading = False
        #: 判読の練習の問題を作る (None when not open), and an exercise an instructor tries once 練習モード has started.
        self.authoring: Optional[ExerciseAuthoring] = None
        self._trial = None
        self.grades: Optional[GradesDialog] = None

    @property
    def practising(self) -> bool:
        return training_io.active() is not None

    # ---- the hooks ---------------------------------------------------------------------------------------

    def start(self) -> None:
        if edition.training_only():
            # MIKAGE 練習版: the shipped settings file's `training` (the screens' settings file is the practice's only).
            self.settings = self._shipped_training()
            return
        # QGIS stopped while practising: what was kept aside of the profile comes back (担当 among it).
        if training_io.recover():
            if self.host.role_keeper is not None:
                self.host.role_keeper.use_stored()
            self.iface.messageBar().pushInfo(tr("練習モード"), tr("前回は練習モードのまま QGIS が終わったため、担当などを練習の前に復元"))
        self.context.configChanged.connect(self._config_read)

    def add_actions(self) -> None:
        self._actions = {
            START: self.entry("training", START, "start_training", name="SatObsTrainingStart"),
            TUTORIAL: self.entry("training_tutorial", TUTORIAL, "open_tutorial", name="SatObsTrainingTutorial"),
            EXERCISES: self.entry("training_exercises", EXERCISES, "open_exercises", name="SatObsTrainingExercises"),
            RESET: self.entry("training_reset", RESET, "reset_training", name="SatObsTrainingReset"),
            END: self.entry("training_end", END, "end_training", name="SatObsTrainingEnd"),
            AUTHOR: self.entry("training_author", AUTHOR, "open_exercise_authoring", name="SatObsTrainingAuthor"),
            GRADES: self.entry("training_grades", GRADES, "open_exercise_grades", name="SatObsTrainingGrades"),
        }
        if edition.training_only():
            self._hide(self.settings, apply=False)  # (the roles applied after the entries are all there)
        self._entries_state()

    def qgis_started(self) -> None:
        # MIKAGE 練習版: 練習モード at once, once QGIS has started.
        if edition.training_only() and not self.practising:
            self.start_training()

    def _shipped_training(self):
        """MIKAGE 練習版: the `training` of the settings file shipped in the zip (the defaults: the built-in sample)."""
        source = settings.production_config_source()[0]
        if not source:
            return DEFAULT_TRAINING
        config, _problems = read_config_file(source)
        return config.training

    def unload(self) -> None:
        # QGIS closing (or the plugin turned off) while practising: back to the production quietly (the map left as it is).
        disconnect(self.context.configChanged, self._config_read)
        self._stop_tutorial()
        self._stop_exercise()
        self._stop_authoring()
        if self.practising:
            training_io.end()
            if self.host.audit is not None:
                self.host.audit.practise(False, {"how": "unload"})
            if getattr(self.host, "feature_use", None) is not None:
                self.host.feature_use.practise(False)
            self._screens(False)
        if self.progress is not None:
            self.progress.close()
            self.progress = None

    # ---- the settings file's `training` -----------------------------------------------------------------

    def _config_read(self) -> None:
        if self.context.loading or self.practising or edition.training_only():
            return
        self.settings = self.context.config.training
        self._hide(self.settings)

    def _hide(self, training, apply: bool = True) -> None:
        """The entries the settings file's `training` does not offer hidden (and MIKAGE 練習版's never there)."""
        hidden = set() if training.enabled else {tr(t) for t in ENTRIES}
        tables = training.exercises
        if tables is None:
            hidden |= {tr(AUTHOR), tr(GRADES)}
        elif not tables.results:
            hidden.add(tr(GRADES))
        if edition.training_only():
            hidden |= {tr(t) for t in NOT_IN_TRAINING_EDITION}
        others = self.host.hidden_entries - {tr(t) for t in ENTRIES}
        if others | hidden != self.host.hidden_entries:
            self.host.hidden_entries = others | hidden
            if apply:
                self.host.apply_roles()

    def _entries_state(self) -> None:
        on = self.practising
        for text, action in self._actions.items():
            action.setEnabled(not self._busy and (True if text in (TUTORIAL, EXERCISES, GRADES) else on if text == END else not on))
        reset = self._actions.get(RESET)
        if reset is not None:
            reset.setEnabled(not self._busy and self.settings.builtin)

    # ---- starting ----------------------------------------------------------------------------------------

    def start_training(self, tutorial: bool = False) -> bool:
        """「練習モードを始める」: asked first; False when it did not start (asked to stop, or already on).
        `tutorial`: the question offers the courses after it (ticked); by 「判読の練習…」, its exercises instead."""
        if self.practising or self._busy:
            return False
        if not self.settings.enabled:
            self.iface.messageBar().pushInfo(tr("練習モード"), tr("練習モードは使えない（管理者の設定: training）"))
            return False
        if edition.training_only():
            return self._start_by_itself(tutorial)
        builtin = self.settings.builtin
        files = training_io.files()
        ask = StartDialog(self.window(), builtin, files.created() if files.ready() else None,
                          from_production=masters.names_text(self.settings.from_production) if builtin else "", tutorial=tutorial,
                          exercises=self._exercises_after_start or self._trial is not None)
        if ask.exec() != StartDialog.DialogCode.Accepted:
            self._trial = None
            return False
        self._tutorial_after_start = ask.tutorial.isChecked()
        if not self._project_saved():
            self._trial = None
            return False
        # (the instructor's windows: the production's tables are not written while practising)
        self._stop_authoring()
        self.project_before = QgsProject.instance().fileName()
        if builtin and (not files.ready() or ask.renew.isChecked()):
            self._prepare(files, reset=files.ready(), then=lambda: self._switch_on(files, builtin))
        else:
            self._switch_on(files, builtin)
        return True

    def _start_by_itself(self, tutorial: bool = False) -> bool:
        """MIKAGE 練習版: 練習モード without the question (the sample made first when it is not there yet)."""
        self._tutorial_after_start = tutorial
        builtin = self.settings.builtin
        files = training_io.files()
        if builtin and not files.ready():
            self._prepare(files, reset=False, then=lambda: self._switch_on(files, builtin))
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
            tr("いまのプロジェクトに未保存の変更あり。練習の前に保存"),
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
        self.progress = QProgressDialog(tr("練習用の画像と背景地図を作成中…"), "", 0, 0, self.window())
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
                self.iface.messageBar().pushWarning(tr("練習モード"), tr("練習データの作成に失敗: {m}").format(m=message))

        run_task(tr("練習データの作成"), work, lambda _ok: finished(True), lambda message: finished(False, message))

    def _production(self) -> Optional[training_io.Production]:
        """
        The production's settings file as read now, with the masters to take from it (None: nothing to take
        from it, or not readable): also for 判読の練習's organisation's exercises (their images may be the
        production catalogs') and its image catalogs shown while practising (imageCatalogs[].mode).
        """
        names = self.settings.from_production
        source = settings.production_config_source()[0]
        if not source:
            return None
        value = None
        try:
            if self.context.source == source and self.context.config_text is not None:
                value = load_jsonc(self.context.config_text)  # (read from a URL)
            elif re.match(r"^https?://", source, re.IGNORECASE):
                cached = settings.cached_config(source)
                value = cached[0] if cached is not None else None
            else:
                with open(source, encoding="utf-8-sig") as f:
                    value = load_jsonc(f.read())
        except (OSError, ValueError):
            value = None
        if not isinstance(value, dict) or not (names or self.settings.exercises is not None or masters.practice_catalog_entries(value)):
            return None
        return training_io.Production(value, source, tuple(names))

    def _switch_on(self, files, builtin: bool) -> None:
        production = self._production()
        try:
            server, taken = training_io.start_server(files, production) if builtin else (None, ())
        except OSError as error:
            self.iface.messageBar().pushWarning(tr("練習モード"), error_text(error))
            return
        source = files.config if builtin else resolve_config(self.settings.config, settings.production_config_source()[0])
        # In the production's audit log, then nothing more until it ends.
        if self.host.audit is not None:
            self.host.audit.practise(True, {"builtin": builtin, "config": "" if builtin else source})
        # 機能の使われ方: nothing counted while practising.
        if getattr(self.host, "feature_use", None) is not None:
            self.host.feature_use.practise(True)
        training_io.begin(training_io.TrainingSession(files, source, builtin, server, production=production,
                                                      from_production=taken))
        self.context.switch_files()
        self.context.reload()
        self._forget_lists()
        # A new project: no production layer left on the map (the one of before saved or let go already).
        # MIKAGE 練習版 keeps the one open: it never had any.
        if not edition.training_only():
            QgsProject.instance().setDirty(False)
            self.iface.newProject(False)
        if builtin:
            self._add_basemap(files)
        self._screens(True)
        self._entries_state()
        read_names = masters.read_text(taken, production.value if production is not None else None)
        read = tr("{m}は本番から読み込み（読むだけ）。").format(m=read_names) if read_names else ""
        if edition.training_only():
            self.iface.messageBar().pushMessage(tr(edition.name()), tr("練習モードで開始。") + read + tr("操作の案内は地図の上の「チュートリアル」"),
                                                Qgis.MessageLevel.Success, 8)
        else:
            self.iface.messageBar().pushMessage(tr("練習モード"), tr("練習モードを開始。") + read + tr("終えるときは地図の上の「練習モードを終える」"),
                                                Qgis.MessageLevel.Success, 8)
        tutorial, exercises, trial = self._tutorial_after_start, self._exercises_after_start, self._trial
        self._tutorial_after_start = self._exercises_after_start = False
        self._trial = None
        # (once the settings file is read and the docks have it; 「判読の練習…」 asked for first)
        if trial is not None:
            QTimer.singleShot(300, lambda: self.run_exercise(trial, [trial], trial=True))
        elif exercises:
            QTimer.singleShot(300, self.open_exercises)
        elif tutorial:
            QTimer.singleShot(300, self.open_tutorial)

    def _add_basemap(self, files) -> None:
        project = QgsProject.instance()
        there = any(os.path.normcase(os.path.abspath(layer.source())) == os.path.normcase(os.path.abspath(files.basemap))
                    for layer in project.mapLayers().values())
        if os.path.exists(files.basemap) and not there:  # (MIKAGE 練習版's project may have it already)
            layer = QgsRasterLayer(files.basemap, tr(BASEMAP_NAME))
            if layer.isValid():
                project.addMapLayer(layer, False)
                project.layerTreeRoot().addLayer(layer)  # (at the bottom)
        self.show_area()
        QgsProject.instance().setDirty(False)

    def show_area(self) -> None:
        """The map on the sample's area."""
        canvas = self.iface.mapCanvas()
        west, south, east, north = AREA
        box = QgsRectangle(west - 0.03, south - 0.02, east + 0.03, north + 0.02)
        crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs.isValid() and crs != wgs84:
            box = QgsCoordinateTransform(wgs84, crs, QgsProject.instance()).transformBoundingBox(box)
        canvas.setExtent(box)
        canvas.refresh()

    # ---- ending ------------------------------------------------------------------------------------------

    def end_training(self) -> bool:
        """「練習モードを終える」: back to the production and its project; False when not practising (or MIKAGE 練習版)."""
        if not self.practising or self._busy or edition.training_only():
            return False
        self._stop_tutorial()
        self._stop_exercise()
        training_io.end()
        self._screens(False)
        if self.host.role_keeper is not None:
            self.host.role_keeper.use_stored()  # (the roles before practising: the profile has them back)
        self.context.switch_files()
        self.context.reload()
        self._forget_lists()
        if self.host.audit is not None:
            self.host.audit.practise(False)
        if getattr(self.host, "feature_use", None) is not None:
            self.host.feature_use.practise(False)
        self._entries_state()
        # The practice's project let go (nothing to keep), the one of before opened again once the settings file is read.
        QgsProject.instance().setDirty(False)
        if self.context.loading:
            self.context.configChanged.connect(self._open_project_before)
        else:
            self._open_project_before()
        self.iface.messageBar().pushMessage(tr("練習モード"), tr("練習モードを終了（練習データは次の練習まで残る）"),
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
            tr("練習データを最初の見本に戻す。練習で記録したもの・依頼・計画ファイルなどは消える"),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return False
        self._stop_tutorial()
        self._stop_exercise()
        session = training_io.active()
        files = training_io.files()
        if session is None:
            self._prepare(files, reset=True, then=lambda: self.iface.messageBar().pushSuccess(tr("練習モード"), tr("練習データを最初に復元")))
            return True
        # Practising: the map and the files let go, the data made again, the server started again on it.
        QgsProject.instance().setDirty(False)
        self.iface.newProject(False)
        if session.server is not None:
            session.server.stop()
        self.context.release_files()

        def again() -> None:
            try:
                session.server, session.from_production = training_io.start_server(files, session.production)
            except OSError as error:
                self.iface.messageBar().pushWarning(tr("練習モード"), error_text(error))
                return
            self.context.switch_files()
            self.context.reload()
            self._forget_lists()
            self._add_basemap(files)
            self.iface.messageBar().pushSuccess(tr("練習モード"), tr("練習データを最初に復元"))

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
                buttons = [(tr(TUTORIAL_BUTTON), self.open_tutorial), (tr(EXERCISES_BUTTON), self.open_exercises),
                           (tr(RESET), self.reset_training)]
                if not edition.training_only():
                    buttons.append((tr(END), self.end_training))
                self.band = TrainingBand(canvas, buttons, tr(EDITION_BAND_TEXT if edition.training_only() else BAND_TEXT),
                                         cover=self.iface.messageBar())
            if self.mark is None:
                self.mark = status_mark()
                window.statusBar().addPermanentWidget(self.mark)
            if self.marks is None:
                self.marks = TitleMarks(window, list(self.docks.values()))
            notes = {tr(t): tr(NOTE) for t in (*NOT_IN_PRACTICE, AUTHOR)}
            session = training_io.active()
            if session is not None and "taxonomy" in session.from_production:
                notes[tr(TAXONOMY_EDIT)] = tr(TAXONOMY_NOTE)
                notes[tr(TAXONOMY_IMPORT)] = tr(TAXONOMY_NOTE)
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

    # ---- チュートリアル -----------------------------------------------------------------------------------

    def open_tutorial(self) -> bool:
        """
        「チュートリアル…」: the courses, and the one chosen started (False: none). Not practising: 練習モード is
        started first (asked as usual), the courses offered once it is on.
        """
        if self._busy:
            return False
        if not self.practising:
            return self.start_training(tutorial=True)
        if self.tutorial is not None:
            self.tutorial.guide.show()
            self.tutorial.guide.raise_()
            return True
        self._stop_exercise()
        found = courses(TutorialPlace(self))
        session = training_io.active()
        keeper = self.host.role_keeper
        dialog = CourseList(found, read_progress(session.files.progress), keeper.codes if keeper is not None else [],
                            {r.code: r.name for r in roles.ROLES}, self.window())
        if dialog.exec() != CourseList.DialogCode.Accepted or dialog.choice is None:
            return False
        key, at = dialog.choice
        self.run_course(next(c for c in found if c.key == key), at)
        return True

    def run_course(self, course, at: int = 0) -> Optional[Tutorial]:
        """A course started at step `at` (its role used too while practising: the roles of before come back after)."""
        session = training_io.active()
        if session is None:
            return None
        self._stop_tutorial()
        self._stop_exercise()
        self._use_role(course.role)
        path = session.files.progress

        def keep(key: str, step: int, finished: bool) -> None:
            try:
                write_progress(path, kept(read_progress(path), key, step, finished))
            except OSError:
                pass

        self.tutorial = Tutorial(course, self.window(), keep, self._open_help, more=lambda: QTimer.singleShot(0, self.open_tutorial))
        self.tutorial.ended.connect(self._tutorial_ended)
        self.tutorial.start(at)
        return self.tutorial

    def _open_help(self, section: str) -> None:
        opener = getattr(self.host, "open_help", None)
        if opener is not None:
            opener(section)

    def _tutorial_ended(self, _key: str, _finished: bool) -> None:
        self.tutorial = None

    def _stop_tutorial(self) -> None:
        """The course put aside (練習モード ending, its data made again, QGIS closing): where it was is kept."""
        if self.tutorial is not None:
            tutorial, self.tutorial = self.tutorial, None
            tutorial.stop()

    def _use_role(self, role: str) -> None:
        """A role used while practising too (the roles of before come back when it ends)."""
        keeper = self.host.role_keeper
        if role and keeper is not None and role not in keeper.codes:
            keeper.choose([*keeper.codes, role])

    # ---- 判読の練習 ---------------------------------------------------------------------------------------

    def exercises(self) -> list:
        """
        The exercises offered: the organisation's (as last read) and the sample's (on the built-in sample, with the
        organisation's scoring).
        """
        session = training_io.active()
        if session is None:
            return []
        sample = []
        if session.builtin:
            try:
                with open(session.files.sample_images, encoding="utf-8") as f:
                    plans = json.load(f)
            except (OSError, ValueError):
                plans = []
            sample = sample_exercises(plans if isinstance(plans, list) else [], self.settings.scoring)
        return [*self.org_exercises, *sample]

    def _user(self) -> str:
        """
        Who practises, as the results table names them: the user name MIKAGE writes in the tables it edits
        (editTracking: the same on every computer), else the name the observation records are made by.
        """
        session = training_io.active()
        production = session.production.value if session is not None and session.production is not None else None
        tracking = None
        if isinstance(production, dict) and isinstance(production.get("editTracking"), dict):
            # (the production's: the practice's settings file has its own)
            tracking = edit_stamp.edit_tracking_of(production["editTracking"], [])
        name = edit_stamp.user_name(tracking)
        if name:
            return name
        try:
            return self.host.api.observation_dock.observer()
        except (AttributeError, KeyError, RuntimeError):
            return ""

    def read_org_exercises(self, then=None) -> bool:
        """
        The organisation's exercises and answers read from its tables (training.exercises), with the person's tries
        in its results table; the tries not sent yet sent first. In a task; `then` once done (read or not). False:
        none to read, or reading already.
        """
        tables, session = self.settings.exercises, training_io.active()
        if tables is None or session is None or self._reading:
            return False
        self._reading = True
        client = self.context.esri()
        path = session.files.exercise_results
        pending = unsent(read_results(path)) if tables.results else []
        user, base = self._user(), self.settings.scoring
        tokens = {u: None for u in (tables.exercises, tables.answers, tables.results) if u}

        def work(task):
            sent = []
            if pending:
                try:
                    ok, _why = send_results(client, tables, pending, tokens[tables.results])
                    sent = [r for r, went in zip(pending, ok) if went]
                except Exception:  # noqa: BLE001 (sent again next time)
                    sent = []
            problems: list[str] = []
            found = read_exercises(client, tables, tokens[tables.exercises], base, problems,
                                   cancelled=task.isCanceled if task is not None else None)
            mine = []
            if tables.results:
                try:
                    mine = read_results_of(client, tables, user, tokens[tables.results])
                except Exception as error:  # noqa: BLE001 (the exercises still offered; the tries of this computer only)
                    problems.append(tr("結果の表の取得に失敗（この PC の結果だけで集計）: {e}").format(e=error_text(error)))
            return found, mine, problems, sent

        def done(result) -> None:
            self._reading = False
            found, mine, problems, sent = result
            if sent:
                try:
                    mark_sent(path, sent)
                except OSError:
                    pass
            self.org_exercises, self.org_results = found, mine
            self.org_problem = ""
            if problems:
                more = tr("ほか {n} 件").format(n=len(problems) - 3) if len(problems) > 3 else ""
                self.iface.messageBar().pushWarning(tr("判読の練習"), tr("組織の問題で気になること: ") + " / ".join(problems[:3]) + more)
            if then is not None:
                then()

        def failed(message: str) -> None:
            self._reading = False
            self.org_problem = tr("組織の問題の取得に失敗: {m}").format(m=message)
            if then is not None:
                then()

        run_task(tr("判読の練習: 組織の問題の取得"), work, done, failed)
        return True

    def all_results(self) -> list:
        """The person's tries: this computer's and the organisation's results table's (another computer's too)."""
        session = training_io.active()
        here = read_results(session.files.exercise_results) if session is not None else []
        return merged_results(here, self.org_results)

    def open_exercises(self) -> bool:
        """
        「判読の練習…」: the exercises, and the one chosen started (False: none). Not practising: 練習モード is
        started first (asked as usual), the list offered once it is on.
        """
        if self._busy:
            return False
        if not self.practising:
            self._exercises_after_start = True
            if not self.start_training():
                self._exercises_after_start = False
                return False
            return True
        run = self.exercise_run
        if run is not None:
            for window in (run.bar, run.review):
                if window is not None:
                    window.show()
                    window.raise_()
            return True
        self._stop_tutorial()
        if self.settings.exercises is not None:
            # (the organisation's, read afresh each time: an instructor may have added some)
            if self._reading:
                return True
            self.iface.messageBar().pushInfo(tr("判読の練習"), tr("組織の問題を取得中…"))
            return self.read_org_exercises(then=self._offer_exercises)
        return self._offer_exercises()

    def _offer_exercises(self) -> bool:
        """The list of the exercises, and the one chosen started (False: none)."""
        if not self.practising or self.exercise_run is not None:
            return False
        found = self.exercises()
        if not found:
            text = self.org_problem or tr("練習できる問題がない（組織の問題も内蔵の見本もない）")
            self.iface.messageBar().pushInfo(tr("判読の練習"), text)
            return False
        keeper = self.host.role_keeper
        org, sample = len(self.org_exercises), len(found) - len(self.org_exercises)
        parts = ([tr("組織の問題 {n} 問").format(n=org)] if org else []) + ([tr("見本の問題（横須賀）{n} 問").format(n=sample)] if sample else [])
        source = "・".join(parts)
        if self.org_problem:
            source += "（" + self.org_problem + "）"
        elif self.settings.exercises is None:
            source += tr("。組織の問題は管理者が登録")
        dialog = ExerciseList(found, self.all_results(), [*(keeper.codes if keeper is not None else []), EXERCISE_ROLE],
                              source, lambda: MyResults(self.all_results(), self.window()).exec(), self.window())
        if dialog.exec() != ExerciseList.DialogCode.Accepted or dialog.choice is None:
            return False
        chosen = next((e for e in found if e.key == dialog.choice), None)
        return chosen is not None and self.run_exercise(chosen, found) is not None

    def run_exercise(self, exercise, among: Optional[list] = None, trial: bool = False) -> Optional[ExerciseRun]:
        """
        An exercise started (in the role that records, used while practising); None when it could not start. `trial`:
        an instructor's (「練習モードで試す」): the try neither kept nor sent, an exam as often as wanted.
        """
        session = training_io.active()
        if session is None:
            return None
        if exercise.exam and not trial and exam_taken(self.all_results(), exercise.key):
            # (an exam once: this computer's tries and the results table's, another computer's too)
            self.iface.messageBar().pushInfo(tr("判読の練習"), tr("「{t}」は試験で、受験済み（1 回だけ）").format(t=exercise.title))
            return None
        self._stop_tutorial()
        self._stop_exercise()
        self._use_role(EXERCISE_ROLE)
        among = among if among is not None else self.exercises()
        keeper = self.host.role_keeper
        mine = {*(keeper.codes if keeper is not None else []), EXERCISE_ROLE}

        def next_of(e):
            # (as the list offers them: those of the person's roles, not an exam taken already)
            if trial:
                return None
            taken = self.all_results()
            same = sorted((x for x in among if x.course == e.course and (not x.roles or mine & set(x.roles))
                           and not (x.exam and exam_taken(taken, x.key))), key=lambda x: (x.order, x.title))
            later = [x for x in same if (x.order, x.title) > (e.order, e.title)]
            return later[0] if later else None

        tables = self.settings.exercises
        run = ExerciseRun(exercise, TutorialPlace(self), session.files.exercise_results, session.files.exercise_records,
                          self.window(), next_of, elsewhere=self._production_image, user=self._user,
                          send=self._send_result if tables is not None and tables.results and not trial else None, others=self.org_results,
                          path_of=self._image_path, trial=trial)
        self.exercise_run = run
        run.ended.connect(lambda: self._exercise_ended(run))
        run.wants.connect(lambda what: self._exercise_next(run, what, next_of))
        if not run.start():
            self.exercise_run = None
            self.iface.messageBar().pushWarning(tr("判読の練習"), run.problem)
            return None
        return run

    def _image_path(self, source: str) -> str:
        """An exercise's image file (IMAGE_SOURCE) on this computer: the settings file's pathMappings (the production's too)."""
        return local_path(source, self.context.config.path_mappings)

    def _production_image(self, image_id: str, then, missing) -> None:
        """
        An exercise's image not in the practice's catalogs: looked up in the production's (read only; in a task)
        and opened from there; `missing` when none has it.
        """
        session = training_io.active()
        production = session.production if session is not None else None
        catalog_dock = getattr(self.host.api, "image_catalog", None)
        if production is None or catalog_dock is None:
            missing()
            return
        config, _problems = parse_config(masters.made_whole(production.value, production.source))
        catalogs = [c for c in (*config.image_catalogs, *config.practice_catalogs) if c.fields.get("id")]
        if not catalogs:
            missing()
            return
        client = self.context.esri()
        asks = [(c, None) for c in catalogs]

        def work(_task):
            for c, token in asks:
                found = records_by_ids(client, c, [image_id], token)
                if found:
                    return c, found[0]
            return None

        def done(result) -> None:
            if result is None:
                missing()
                return
            catalog, record = result
            layers = catalog_dock.open_catalog_records(catalog, [record])
            if layers:
                then(layers)
            else:
                missing()

        run_task(tr("判読の練習: 本番の画像カタログで検索"), work, done, lambda _message: missing())

    def _send_result(self, record: dict, said) -> None:
        """A try sent to the organisation's results table (in a task); `said` gets how it went."""
        tables, session = self.settings.exercises, training_io.active()
        if tables is None or not tables.results or session is None:
            return
        client, token, path = self.context.esri(), None, session.files.exercise_results

        def work(_task):
            return send_results(client, tables, [record], token)

        def done(result) -> None:
            ok, why = result
            if ok and ok[0]:
                try:
                    mark_sent(path, [record])
                except OSError:
                    pass
                self.org_results = [*self.org_results, {**record, "fromTable": True}]
                said(tr("結果を組織の表に送信"))
            else:
                failed(" / ".join(why) or tr("追加に失敗"))

        def failed(message: str) -> None:
            said(tr("組織の表への送信に失敗（次に「判読の練習…」を開くときに送り直し）: {m}").format(m=message))

        run_task(tr("判読の練習: 結果の送信"), work, done, failed)

    def _exercise_ended(self, run: ExerciseRun) -> None:
        if self.exercise_run is run:
            self.exercise_run = None
        if run.outcome is not None:
            self.iface.messageBar().pushInfo(tr("判読の練習"), result_line(run.outcome))

    def _exercise_next(self, run: ExerciseRun, what: str, next_of) -> None:
        after = next_of(run.exercise) if what == "next" else None
        if after is not None:
            QTimer.singleShot(0, lambda: self.run_exercise(after))
        else:
            QTimer.singleShot(0, self.open_exercises)

    def _stop_exercise(self) -> None:
        """The exercise put aside (another started, 練習モード ending, QGIS closing): not submitted, nothing kept."""
        if self.exercise_run is not None:
            run, self.exercise_run = self.exercise_run, None
            run.stop()

    # ---- 判読の練習の問題を作る・成績 (the instructors') -----------------------------------------------------

    def open_exercise_authoring(self) -> bool:
        """「判読の練習の問題を作る…」: the organisation's exercises to change and make (the production's tables; not while practising)."""
        if self._busy:
            return False
        if self.practising:
            self.iface.messageBar().pushInfo(tr("判読の練習"), tr("問題は本番の MIKAGE で作成（練習モードを終えてから）"))
            return False
        if self.settings.exercises is None:
            self.iface.messageBar().pushInfo(tr("判読の練習"), tr("問題の表と正解の表が未設定（管理者に連絡: training.exercises）［E703］"))
            return False
        if self.authoring is not None:
            self.authoring.manager.show()
            self.authoring.manager.raise_()
            if self.authoring.editor is not None:
                self.authoring.editor.raise_()
            return True
        self.authoring = authoring = ExerciseAuthoring(TutorialPlace(self), ExerciseTablesIO(self), self.window())
        authoring.ended.connect(lambda: self._authoring_ended(authoring))
        authoring.open()
        return True

    def _authoring_ended(self, authoring: ExerciseAuthoring) -> None:
        if self.authoring is authoring:
            self.authoring = None

    def _stop_authoring(self) -> None:
        if self.authoring is not None:
            authoring, self.authoring = self.authoring, None
            authoring.stop()

    def try_exercise(self, exercise) -> bool:
        """「練習モードで試す」: an exercise solved as the people will, in 練習モード (started first: asked as usual)."""
        if self.practising:
            return self.run_exercise(exercise, [exercise], trial=True) is not None
        self._trial = exercise
        if not self.start_training():
            self._trial = None
            return False
        return True

    def open_exercise_grades(self, then=None) -> bool:
        """
        「判読の練習の成績…」: everyone's results of the organisation's results table, by course (read in a task, then
        shown; `then(dialog)` instead of showing it, for the tests). False: no table, or reading already.
        """
        tables = self.settings.exercises
        if tables is None or not tables.results:
            self.iface.messageBar().pushInfo(tr("判読の練習"), tr("結果の表が未設定（管理者に連絡: training.exercises.results）［E703］"))
            return False
        if self._reading:
            return False
        self._reading = True
        client = self.context.esri()
        tokens = {u: None for u in (tables.exercises, tables.answers, tables.results)}
        self.iface.messageBar().pushInfo(tr("判読の練習"), tr("結果の表を取得中…"))

        def work(task):
            cancelled = task.isCanceled if task is not None else None
            results = read_all_results(client, tables.results, tokens[tables.results], cancelled=cancelled)
            note = ""
            try:
                exercises = client.query_all(tables.exercises, "1=1", tokens[tables.exercises], geometry=False, cancelled=cancelled,
                                             limit=20_000)
            except Exception as error:  # noqa: BLE001 (the results still shown, their exercises by their titles)
                exercises, note = [], tr("問題の表の取得に失敗（結果にある問題だけ表示）: {e}").format(e=error_text(error))
            return results, exercises, note

        def done(result) -> None:
            self._reading = False
            results, rows, note = result
            columns = [Column(d.key, d.title or d.id, d.course, d.order, d.mode, d.pass_score) for d in drafts_of(rows, [])]
            dialog = GradesDialog(results, columns, note, self.window())
            self.grades = dialog
            if then is not None:
                then(dialog)
            else:
                dialog.exec()
                self.grades = None

        def failed(message: str) -> None:
            self._reading = False
            self.iface.messageBar().pushWarning(tr("判読の練習"), tr("結果の表の取得に失敗: {m}").format(m=message))

        run_task(tr("判読の練習: 成績の取得"), work, done, failed)
        return True
