"""
判読の練習 (練習モードの段階 3): one exercise solved, from its start to its
review (features/training.py starts it from the list).

While it runs the observation dock records to a store of its own (the
practice folder's `exercise_records.gpkg`, emptied when it starts: the
answers apart from the practice's records, and nothing else's records shown
on its image); its image (and the one before, 変化を見つける) is opened, the
map moved to its area, its area and the things to look at marked
(ui/exercise_map.py); a small window says what to do and the time
(ui/exercise_screens.py). 提出 scores the records made against the answers
(core/training/exercises.py), keeps the try (results.py) and shows the review
over the map, the answers marked where they are. When it ends the dock's
stores are the settings file's again and the images opened for it are taken
off the map.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Optional

from qgis.core import QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QPixmap

from ..core.gpkg import open_local
from ..qgis_io.tasks import run_task
from ..core.obs.api import kind_of, LocalAreaStore, LocalObservationStore, StoreSet
from ..core.training.api import (
    add_result, CAME, CHANGE, Exercise, fitted, GONE, hint, IDENTIFY, Mark, MEASURE, MISSED, MISSED_BONUS, Outcome, read_results,
    result_record, RIGHT, score, STAYED, tries_of,
)
from .exercise_map import ExerciseMarks, fit_into, free_part, pixel_of, Spot
from .exercise_screens import BAD, ExerciseBar, ReviewWindow
from .tutorial_courses import show

#: The observation store's name while an exercise runs.
LABEL = "判読の練習"
#: The clicks a record takes in 測る (rear, front, a side: length, heading and width).
MEASURE_POINTS = 3
#: Said in the AI結果 dock while an exercise is solved (its results would tell the answers).
AI_WITHHELD = "判読の練習の間は、AI の結果を出しません（答えが分かってしまうため）"
#: The map's margin round the area (a share of its size).
MARGIN = 0.08


class ExerciseRecords(LocalObservationStore):
    """The exercise's records (a GeoPackage of their own)."""

    label = LABEL


class FixedClasses:
    """The practice's classes as read when the exercise started (the exercise's store reads them; they are not changed)."""

    label = LABEL

    def __init__(self, taxa, fields):
        self.taxa, self.fields = taxa, fields

    def load(self):
        return list(self.taxa) if self.taxa is not None else None

    def load_fields(self):
        return list(self.fields) if self.fields is not None else None

    def save(self, taxa) -> None:
        raise ValueError("判読の練習の間は分類を変えられません")

    def save_fields(self, fields) -> None:
        raise ValueError("判読の練習の間は分類を変えられません")


def marks_of(records: list, exercise: Exercise, before_things: set) -> list[Mark]:
    """
    The records made for the exercise as scored: those of its image, not deleted and not 「不可」; for
    変化を見つける, a thing of the image before carried on (継続), gone (消失) or new (来た).
    """
    out = []
    for o in records:
        if o.deleted_at is not None or o.image_id != exercise.image_id or o.unseen:
            continue
        change = ""
        if exercise.type == CHANGE:
            change = GONE if not o.present else STAYED if o.object_id in before_things else CAME
        elif not o.present:
            continue
        out.append(Mark(o.id, o.lon, o.lat, o.kind, o.activity, o.length, o.width, o.heading, change, o.image_id))
    return out


class ExerciseRun(QObject):
    """
    One exercise being solved, then reviewed. `place` reaches the plugin (features/training.py's TutorialPlace:
    `api`, `canvas`, `iface`); `next_of(exercise)` is the one after it in its course (None at its end).
    """

    #: Closed for good (its windows and marks gone, the dock's stores back).
    ended = pyqtSignal()
    #: What the person asked for at the review: "next" (the next exercise), "list" (the list again).
    wants = pyqtSignal(str)

    def __init__(self, exercise: Exercise, place: Any, results_path: str, records_path: str, main=None,
                 next_of: Callable[[Exercise], Optional[Exercise]] = lambda _e: None):
        super().__init__()
        self.exercise = exercise
        self.place = place
        self.results_path = results_path
        self.records_path = records_path
        self.main = main
        self.next_of = next_of
        self.obs = place.api.observation_dock
        self.catalog = place.api.image_catalog
        self.canvas = place.canvas
        self.marks = ExerciseMarks(self.canvas)
        self.bar: Optional[ExerciseBar] = None
        self.review: Optional[ReviewWindow] = None
        self.outcome: Optional[Outcome] = None
        self.started: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.hints = 0
        #: The things recorded on the image before by the exercise (変化を見つける: what 継続 and 消失 are of).
        self.before_things: set = set()
        self.layers: list[str] = []
        self.db = None
        self.store: Optional[ExerciseRecords] = None
        #: The practice store's classes and words (the exercise's store uses them).
        self._base: Optional[tuple] = None
        self._points_before: Optional[int] = None
        self._ended = False
        self.problem = ""
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)

    # ---- starting ----------------------------------------------------------------------------------------

    def start(self) -> bool:
        """
        The exercise opened: the practice's classes read (in a task), then its images opened in turn; the window
        shows once they are. False: it cannot start.
        """
        obs = self.obs
        if obs is None or self.catalog is None:
            self.problem = "観測記録・画像カタログのドックが使えません（担当に「観測記録」が要ります）"
            return False
        if self._base is not None:
            self._begin()
            return True
        current = obs.store_set
        if current is None:
            self.problem = "観測記録の保存先がありません"
            return False
        classes = current.taxonomy

        def read(_task):
            return classes.load(), classes.load_fields()

        def done(result) -> None:
            if self._ended:
                return
            taxa, fields = result if isinstance(result, tuple) else (None, None)
            self._base = (FixedClasses(taxa, fields), current.vocabulary)
            self._begin()

        def failed(message: str) -> None:
            # (the classes of the settings file then, as the dock does)
            if not self._ended:
                self._base = (FixedClasses(None, None), current.vocabulary)
                self._begin()

        run_task("判読の練習: 分類の読み込み", read, done, failed)
        return True

    def _ai(self, why: str) -> None:
        """The AI結果 dock's results withheld (`why`) or shown again ("")."""
        try:
            dock = self.place.api.ai_results
        except (AttributeError, KeyError, RuntimeError):
            return
        if dock is not None:
            dock.withhold(why)

    def _begin(self) -> None:
        obs = self.obs
        self._ai(AI_WITHHELD)
        self.db = self.db or open_local(self.records_path)
        self.store = ExerciseRecords(self.db)
        self.store.clear()
        areas = LocalAreaStore(self.db, LABEL)
        areas.clear()
        obs.use_store(StoreSet(LABEL, self.store, areas, self._base[0], self._base[1], None))
        show(obs)
        e = self.exercise
        images = ([(e.before_image_id, e.before_image_source)] if e.type == CHANGE and (e.before_image_id or e.before_image_source) else [])
        images.append((e.image_id, e.image_source))
        self._open(images, self._opened)

    def _open(self, images: list, then: Callable[[], None]) -> None:
        """The images opened one after another (by their id in the catalogs, else their file), then `then`."""
        if not images:
            then()
            return
        (image_id, source), rest = images[0], images[1:]
        known = self._layer_of(image_id)
        if known is not None:
            self._open(rest, then)
            return
        if image_id:
            def opened(layers) -> None:
                self.layers.extend(x.id() for x in layers or [] if hasattr(x, "id"))
                self._open(rest, then)

            # (from the catalogs, not asked whether to open the files of last time: the exercise's image as it is)
            open_id = getattr(self.catalog, "open_from_catalogs", None) or self.catalog.open_image_id
            open_id(image_id, then=opened, missing=lambda: self._fail(f"画像 {image_id} が画像カタログにありません"))
            return
        layer = QgsRasterLayer(source, os.path.splitext(os.path.basename(source))[0] or source)
        if not layer.isValid():
            self._fail(f"画像を開けません: {source}")
            return
        QgsProject.instance().addMapLayer(layer)
        self.layers.append(layer.id())
        self._open(rest, then)

    def _layer_of(self, image_id: str) -> Optional[str]:
        """The layer of an image opened already for this exercise (another try)."""
        project = QgsProject.instance()
        for lid in self.layers:
            layer = project.mapLayer(lid)
            if layer is not None and image_id and image_id in layer.name():
                return lid
        return None

    def _fail(self, text: str) -> None:
        self.problem = text
        if self.bar is not None:
            self.bar.say(text, BAD)
        elif self.place.iface is not None:
            self.place.iface.messageBar().pushWarning("判読の練習", text)
        self.stop()

    def _opened(self) -> None:
        if self._ended:
            return
        obs, e = self.obs, self.exercise
        if e.type == CHANGE:
            self._make_before()
        obs.go_to(e.image_id)
        self.marks.show_marks(e.area, self.targets())
        if e.type == MEASURE and obs.points != MEASURE_POINTS:
            self._points_before = obs.points
            obs.set_points(MEASURE_POINTS)
        if not obs.active:
            obs.record_button.setChecked(True)
        obs.render()
        self.started = datetime.now()
        self.finished_at = None
        self.hints = 0
        self.bar = ExerciseBar(e, self.main)
        self.bar.submit_pressed.connect(self.submit)
        self.bar.quit_pressed.connect(self.stop)
        self.bar.closed.connect(self._bar_closed)
        self.bar.hint_pressed.connect(self.show_hint)
        self.bar.area_pressed.connect(self.show_area)
        self.tick()
        self.bar.show()
        self.bar.place(self.canvas, self.place.band)
        self.show_area()
        self.timer.start()

    def _make_before(self) -> None:
        """変化を見つける: the records of the image before (what was there), as someone recorded them before."""
        book, e = self.obs.book, self.exercise
        before = e.before_image_id
        if book is None or not before:
            return
        image = next((i for i in book.images if i.id == before), None)
        if image is None:
            return
        kinds = self._kinds()
        exercise = fitted(e, kinds) if kinds else e
        self.before_things = set()
        for a in exercise.answers:
            if a.change not in (STAYED, GONE):
                continue
            record = book.add(a.lon, a.lat, image=before, kind=a.kind or "", activity=a.activity or "", confidence="",
                              note="問題の用意（前の画像の記録）")
            if record is not None:
                self.before_things.add(record.object_id)

    def targets(self) -> list[Spot]:
        """The things to look at, numbered (見分ける・測る); none for the others (they are to be found)."""
        e = self.exercise
        if e.type not in (IDENTIFY, MEASURE):
            return []
        return [Spot(a.lon, a.lat, str(a.number)) for a in e.answers]

    def show_area(self) -> None:
        e = self.exercise
        points = [p for ring in e.area for p in ring] or [(a.lon, a.lat) for a in e.answers]
        if not points:
            return
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        dx, dy = (max(xs) - min(xs)) * MARGIN or 0.005, (max(ys) - min(ys)) * MARGIN or 0.005
        fit_into(self.canvas, min(xs) - dx, min(ys) - dy, max(xs) + dx, max(ys) + dy, self.free())

    def free(self):
        """The part of the map the exercise's windows leave."""
        return free_part(self.canvas, [self.place.band, self.bar, self.review])

    def go_to_spot(self, s: Spot, d: float, always: bool = False) -> None:
        """The map moved round a spot (`d`: degrees of latitude each side) unless it shows beside the windows already."""
        if not always:
            at = pixel_of(self.canvas, s.lon, s.lat)
            if at is not None and self.free().adjusted(16, 16, -16, -16).contains(at.toPoint()):
                return
        fit_into(self.canvas, s.lon - d * 1.4, s.lat - d, s.lon + d * 1.4, s.lat + d, self.free())

    # ---- while it is solved ------------------------------------------------------------------------------

    def seconds(self) -> float:
        end = self.finished_at or datetime.now()
        return (end - self.started).total_seconds() if self.started else 0.0

    def left(self) -> Optional[float]:
        limit = self.exercise.time_limit
        return None if not limit else limit * 60 - self.seconds()

    def tick(self) -> None:
        if self.bar is None:
            return
        left = self.left()
        self.bar.show_time(self.seconds(), left)
        if left is not None and left <= 0 and self.outcome is None:
            self.submit(forced=True)

    def records(self) -> list:
        try:
            return self.store.load() if self.store is not None else []
        except Exception:  # noqa: BLE001 (the file could not be read: nothing recorded)
            return []

    def current_marks(self) -> list[Mark]:
        return marks_of(self.records(), self.exercise, self.before_things)

    def show_hint(self) -> None:
        if self.bar is None or self.exercise.exam:
            return
        self.hints += 1
        self.bar.say(hint(self.exercise, self.current_marks()), "#1d4ed8")

    def _kinds(self) -> list[str]:
        book = self.obs.book if self.obs is not None else None
        try:
            return [kind_of(t.path) for t in book.taxonomy()] if book is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _activities(self, kinds: list[str]) -> Optional[dict]:
        book = self.obs.book if self.obs is not None else None
        if book is None:
            return None
        out = {}
        for k in kinds:
            words, _at = book.activities_for(k)
            out[k] = list(words)
        return out

    # ---- 提出 and the review -----------------------------------------------------------------------------

    def submit(self, forced: bool = False) -> Optional[Outcome]:
        """The records scored and the try kept, the review shown (`forced`: the time is up, a form open is let go)."""
        if self.outcome is not None or self.started is None:
            return self.outcome
        obs = self.obs
        if obs.form.isVisible():
            if not forced:
                if self.bar is not None:
                    self.bar.say("記録のフォームが開いています。「記録」か「取消」をしてから提出してください。", BAD)
                return None
            obs.close_form()
        self.finished_at = datetime.now()
        self.timer.stop()
        kinds = self._kinds()
        exercise = fitted(self.exercise, kinds, self._activities(kinds)) if kinds else self.exercise
        self.outcome = score(exercise, self.current_marks())
        attempt = len(tries_of(read_results(self.results_path), self.exercise.key)) + 1
        record = result_record(self.outcome, self.started, self.finished_at, attempt, obs.observer(), self.hints)
        note = ""
        try:
            add_result(self.results_path, record)
        except OSError as error:
            note = f"結果をこの PC に残せませんでした: {error}"
        if forced:
            note = ("時間になったので提出しました。" + note).strip()
        if obs.active:
            obs.record_button.setChecked(False)
        if self.bar is not None:
            bar, self.bar = self.bar, None
            bar.closed.disconnect(self._bar_closed)
            bar.close()
        self.show_review(note)
        return self.outcome

    def review_spots(self) -> list[Spot]:
        o = self.outcome
        if o is None or not o.exercise.show_answers:
            return []
        out = []
        for item in o.items:
            a = item.answer
            style = "right" if item.state == RIGHT else "missed" if item.state in (MISSED, MISSED_BONUS) else "wrong"
            label = str(a.number) if item.state == RIGHT else f"{a.number} {item.state}"
            out.append(Spot(a.lon, a.lat, label, style))
        for m in o.extras:
            out.append(Spot(m.lon, m.lat, "誤り", "extra"))
        return out

    def show_review(self, note: str = "") -> None:
        o = self.outcome
        has_next = self.next_of(self.exercise) is not None
        self.review = ReviewWindow(o, self.seconds(), has_next, self.main, reference=self._reference, note=note)
        self.review.chosen.connect(self._row_chosen)
        self.review.again_pressed.connect(self.again)
        self.review.next_pressed.connect(lambda: self._leave("next" if has_next else "list"))
        self.review.missed_pressed.connect(self._to_missed)
        self.review.closed.connect(self.stop)
        spots = self.review_spots()
        self.marks.show_marks(self.exercise.area, spots)
        self.review.show()
        self.review.place(self.canvas, self.place.band)
        # (the whole area beside the window: how it came out at a look)
        self.show_area()
        rows = self.review.table.selectionModel().selectedRows() if o.exercise.show_answers else []
        if rows:
            self._row_chosen(rows[0].row())

    @staticmethod
    def _reference(path: str) -> Optional[QPixmap]:
        if path and os.path.isfile(path):
            pixmap = QPixmap(path)
            return pixmap if not pixmap.isNull() else None
        return None

    def _row_chosen(self, index: int) -> None:
        spots = self.review_spots()
        if not 0 <= index < len(spots):
            return
        self.marks.show_marks(self.exercise.area, spots, chosen=index)
        # (the map moved only when the place is not seen beside the window, else left as the person set it)
        self.go_to_spot(spots[index], 0.006)

    def _to_missed(self) -> None:
        if self.review is None or self.outcome is None:
            return
        missed = [i for i, item in enumerate(self.outcome.items) if not item.found]
        if not missed:
            return
        # (one after another: the next after the row chosen)
        rows = self.review.table.selectionModel().selectedRows()
        now = rows[0].row() if rows else -1
        index = next((i for i in missed if i > now), missed[0])
        self.review.select(index)
        self.go_to_spot(self.review_spots()[index], 0.004, always=True)

    def again(self) -> None:
        """もう一度 (練習): the same exercise from the start (its records emptied, the images left open)."""
        if self.exercise.exam:
            return
        self._close_review()
        self.outcome = None
        self.before_things = set()
        self.start()

    def _leave(self, what: str) -> None:
        self.stop()
        self.wants.emit(what)

    def _close_review(self) -> None:
        if self.review is not None:
            review, self.review = self.review, None
            review.closed.disconnect(self.stop)
            review.close()

    def _bar_closed(self) -> None:
        # (× of the window: as やめる)
        self.bar = None
        self.stop()

    # ---- ending ------------------------------------------------------------------------------------------

    def stop(self) -> None:
        """Ended (やめる, the review closed, 練習モード ending): the windows and marks gone, the dock as it was."""
        if self._ended:
            return
        self._ended = True
        self.timer.stop()
        if self.bar is not None:
            bar, self.bar = self.bar, None
            try:
                bar.closed.disconnect(self._bar_closed)
            except TypeError:
                pass
            bar.close()
        self._close_review()
        self.marks.remove()
        obs = self.obs
        if obs is not None:
            try:
                if obs.active:
                    obs.record_button.setChecked(False)
                if self._points_before is not None:
                    obs.set_points(self._points_before)
                obs.use_store(None)
            except RuntimeError:  # (QGIS closing)
                pass
        self._ai("")
        if self.db is not None:
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                pass
            self.db = None
        project = QgsProject.instance()
        gone = [lid for lid in self.layers if project.mapLayer(lid) is not None]
        if gone:
            project.removeMapLayers(gone)
        self.layers = []
        self.ended.emit()


def result_line(outcome: Outcome) -> str:
    """The try in one line (the message bar)."""
    verdict = "合格" if outcome.passed else "不合格"
    return f"{outcome.exercise.title}: {outcome.score:g} 点（{verdict}）"

