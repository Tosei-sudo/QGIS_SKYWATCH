"""
判読の練習 (練習モードの段階 3: ui/exercise_run.py, ui/exercise_screens.py,
features/training.py) in QGIS: each sample exercise answered as a person does
— clicks on the map, the kind and activity chosen in the observation dock's
form, 3 clicks to measure, 継続・消失 on the things of the image before —
then submitted, scored, kept and reviewed; the dock's stores and the map as
they were after; the exam's time and its one try; the practice started first.
"""
import math

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core.training.api import CHANGE, GONE, IDENTIFY, MEASURE, read_results, STAYED  # noqa: E402
from mikage.core.training.exercises import path_of  # noqa: E402
from mikage.qgis_io import training_io  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_training import _feature, made, plugin  # noqa: E402,F401 (the fixtures)
from mikage.ui import exercise_screens, open_again_dialog, training_mode  # noqa: E402
from mikage.ui.exercise_map import pixel_of  # noqa: E402
from mikage.ui.exercise_run import LABEL  # noqa: E402

M_LAT = 111_320.0


@pytest.fixture
def practising(plugin):
    p, iface = plugin
    iface.window.resize(1500, 950)
    training = _feature(p)
    assert p.start_training()
    assert wait_for(lambda: not p.context.loading)
    yield p, iface, training
    training.end_training()


def started(training, key: str):
    found = training.exercises()
    exercise = next(e for e in found if e.id == key)
    run = training.run_exercise(exercise, found)
    assert run is not None, key
    assert wait_for(lambda: run.bar is not None, 15000), run.problem
    # (recording starts once the image is one's own: 分担, in a task)
    assert wait_for(lambda: training.host.api.observation_dock.active, 10000)
    return run


def along(lon: float, lat: float, heading: float, metres: float) -> tuple:
    a = math.radians(heading)
    return (lon + metres * math.sin(a) / (M_LAT * math.cos(math.radians(lat))), lat + metres * math.cos(a) / M_LAT)


def across(lon: float, lat: float, heading: float, metres: float) -> tuple:
    return along(lon, lat, heading + 90, metres)


def answer(p, run, a, kind=None, activity=None) -> None:
    """One thing recorded as a person does (the form filled and 記録 pressed)."""
    obs = p.api.observation_dock
    e = run.exercise
    if e.type == CHANGE and a.change in (STAYED, GONE):
        ghosts = [m for m in obs.scene().marks if m.role == "ghost"]
        ghost = min(ghosts, key=lambda m: (m.lon - a.lon) ** 2 + (m.lat - a.lat) ** 2)
        obs.map_clicked(ghost.lon, ghost.lat, ghost)
        assert obs.form.isVisible()
        obs.submit(a.change == STAYED)
    else:
        if e.type == MEASURE:
            rear = along(a.lon, a.lat, a.heading, -a.length / 2)
            front = along(a.lon, a.lat, a.heading, a.length / 2)
            side = across(a.lon, a.lat, a.heading, a.width / 2)
            for x, y in (rear, front, side):
                obs.map_clicked(x, y, None)
        else:
            obs.map_clicked(a.lon, a.lat, None)
        assert obs.form.isVisible()
        obs.choose_kind(path_of(kind if kind is not None else a.kind))
        word = activity if activity is not None else a.activity
        if word:
            obs.pick_activity(word)
        obs.submit()
    assert wait_for(lambda: not obs.form.isVisible())


@pytest.mark.parametrize("key", ["sample-find", "sample-identify", "sample-measure", "sample-change", "sample-sar"])
def test_each_sample_exercise_answered_right_is_full_marks(practising, key):
    p, _iface, training = practising
    obs = p.api.observation_dock
    shown = p.api.ai_results.image_id
    run = started(training, key)
    e = run.exercise
    # Its own store, its image recorded on, recording on; 測る with 3 clicks.
    assert obs.store_select.currentText() == LABEL and obs.book.current.id == e.image_id and obs.active
    assert (obs.points == 3) == (e.type == MEASURE)
    assert len(run.marks.spots) == (len(e.answers) if e.type in (IDENTIFY, MEASURE) else 0) and run.marks.area
    # The AI's results not shown (they would tell the answers); the area beside the bar, not under it.
    assert p.api.ai_results.withheld and not [x for x in QgsProject.instance().mapLayers().values() if x.name().startswith("外部AI結果")]
    free = run.free()
    assert free.width() < run.canvas.width() or free.height() < run.canvas.height()
    for x, y in e.area[0]:
        assert free.adjusted(-2, -2, 2, 2).contains(pixel_of(run.canvas, x, y).toPoint()), (free, x, y)
    if e.type == CHANGE:
        assert len([m for m in obs.scene().marks if m.role == "ghost"]) == len([a for a in e.answers if a.change in (STAYED, GONE)])
    for a in e.answers:
        answer(p, run, a)
    outcome = run.submit()
    assert outcome is not None and outcome.score == 100, [(i.answer.number, i.state, i.credits) for i in outcome.items]
    assert run.review is not None and run.review.isVisible() and run.review.score.text() == "100 点"
    kept = read_results(training_io.active().files.exercise_results)
    assert [r["exercise"] for r in kept] == [e.key] and kept[0]["score"] == 100
    # Closed: the dock's stores and the map as they were.
    before = len(QgsProject.instance().mapLayers())
    run.review.close()
    assert training.exercise_run is None and obs.store_select.currentText() != LABEL and not obs.active
    assert obs.points == 1 and len(QgsProject.instance().mapLayers()) < before
    # (the AI's results as before: of the image shown before)
    assert not p.api.ai_results.withheld and p.api.ai_results.image_id == shown
    assert not [x for x in QgsProject.instance().mapLayers().values() if x.name().startswith("外部AI結果")]


def test_misses_wrong_kinds_and_records_of_nothing_are_reviewed(practising):
    p, _iface, training = practising
    obs = p.api.observation_dock
    run = started(training, "sample-find")
    a = run.exercise.answers
    answer(p, run, a[0])
    answer(p, run, a[1], kind="船舶 > 貨物船")
    answer(p, run, a[2])
    # Something recorded where there is no ship (in the area, a ship's class): 誤り.
    box = obs.book.current.box
    sea = ((box[0] + box[2]) / 2 + 0.004, box[1] + (box[3] - box[1]) * 0.12)
    obs.map_clicked(*sea, None)
    obs.choose_kind(["船舶"])
    obs.submit()
    assert wait_for(lambda: not obs.form.isVisible())
    run.show_hint()
    assert "あと 1 隻" in run.bar.state.text()
    # A form open is not submitted over.
    obs.map_clicked(a[3].lon, a[3].lat, None)
    assert run.submit() is None and "フォームが開いている" in run.bar.state.text()
    obs.close_form()
    outcome = run.submit()
    assert [i.state for i in outcome.items] == ["正しい", "種別ちがい", "正しい", "見落とし"] and len(outcome.extras) == 1, [(i.answer.kind, i.mark and i.mark.kind, i.credits) for i in outcome.items]
    review = run.review
    assert review.table.rowCount() == 5 and review.to_missed.isVisible()
    assert [s.style for s in run.marks.spots] == ["right", "wrong", "right", "missed", "extra"]
    # The first row not right chosen: its explanation shown.
    assert "解説（2." in review.explanation.text()
    review.missed_pressed.emit()
    assert run.marks.chosen == 3
    kept = read_results(training_io.active().files.exercise_results)[0]
    assert kept["hints"] == 1 and kept["extra"] == 1 and kept["missed"] == 1
    # もう一度: the same exercise from the start (its records gone).
    review.again.click()
    assert wait_for(lambda: run.bar is not None) and run.outcome is None and wait_for(lambda: obs.active, 10000)
    assert [o for o in run.records() if o.deleted_at is None] == []
    # 次の問題へ (after this try): the next of the course.
    for x in run.exercise.answers:
        answer(p, run, x)
    run.submit()
    run.review.next.click()
    assert wait_for(lambda: training.exercise_run is not None and training.exercise_run is not run)
    assert wait_for(lambda: training.exercise_run.bar is not None, 15000)
    assert training.exercise_run.exercise.id == "sample-identify"
    training.exercise_run.stop()


def test_the_exam_has_no_hint_one_try_and_its_time(practising, monkeypatch):
    p, _iface, training = practising
    found = training.exercises()
    exam = next(e for e in found if e.exam)
    exam.time_limit = 0.03  # (about 2 seconds)
    run = training.run_exercise(exam, found)
    assert wait_for(lambda: run.bar is not None, 15000) and wait_for(lambda: p.api.observation_dock.active, 10000)
    assert not run.bar.hint.isVisible() and "残り" in run.bar.time.text()
    answer(p, run, exam.answers[0])
    # The time up: submitted by itself.
    assert wait_for(lambda: run.outcome is not None, 8000)
    assert run.review is not None and not run.review.again.isVisible() and "時間になったので提出" in run.review.note.text()
    kept = read_results(training_io.active().files.exercise_results)
    assert kept[-1]["mode"] == "試験" and kept[-1]["found"] == 1
    run.review.close()
    # Taken: not started again from the list.
    dialogs = []

    def look(self):
        dialogs.append(self)
        row = next(r for r in range(self.table.rowCount()) if self.table.item(r, 1).text() == exam.title)
        self.table.selectRow(row)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(exercise_screens.ExerciseList, "exec", look)
    assert not training.open_exercises()
    d = dialogs[0]
    assert d.table.rowCount() == 6 and not d.start.isEnabled() and "受験済み" in d.detail.text()
    assert d.table.item(5, 7).text() == "済（1 回だけ）"


def test_exercises_start_the_practice_first(plugin, monkeypatch):
    p, _iface = plugin
    training = _feature(p)
    offered, asked = [], []
    monkeypatch.setattr(exercise_screens.ExerciseList, "exec",
                        lambda self: offered.append(self.table.rowCount()) or QDialog.DialogCode.Rejected)
    # (the question says the exercises come after it, not the tutorial's courses)
    monkeypatch.setattr(training_mode.StartDialog, "exec",
                        lambda self: asked.append(self.tutorial.isChecked() or not self.tutorial.isHidden()) or QDialog.DialogCode.Accepted)
    assert training_io.active() is None
    assert training.open_exercises()
    assert training_io.active() is not None and asked == [False]
    assert wait_for(lambda: offered, 5000) and offered == [6]
    assert training.tutorial is None
    training.end_training()


def test_an_image_opened_before_opens_without_the_question(practising, monkeypatch):
    p, _iface, training = practising
    asked = []
    monkeypatch.setattr(open_again_dialog, "ask_open_again", lambda *a: asked.append(a) or None)
    run = started(training, "sample-identify")
    run.stop()
    # The same image again (開いた画像の履歴 has it now): opened as the exercise's, not asked about.
    again = started(training, "sample-change")
    assert asked == [] and p.api.observation_dock.book.current.id == again.exercise.image_id
    again.stop()
