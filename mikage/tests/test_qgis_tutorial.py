"""
チュートリアル (練習モードの段階 2: ui/tutorial_guide.py, ui/tutorial_courses.py,
features/training.py) in QGIS: every course run to its end on the practice
data by its own 「やって見せる」 (each step done as the screen shows it, the
next one coming by itself), its progress kept; 「チュートリアル…」 starts
練習モード first when not practising; the course's role is used while it runs.
"""
import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QTimer  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core.training.api import read_progress  # noqa: E402
from mikage.qgis_io import training_io  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_training import _feature, made, plugin  # noqa: E402,F401 (the fixtures)
from mikage.ui import tutorial_courses, tutorial_guide  # noqa: E402
from mikage.ui.tutorial_courses import courses, question_yes  # noqa: E402

COURSES = {"first": 8, "obs": 19, "request": 16, "layer": 4, "plan": 8}


@pytest.fixture
def practising(plugin, monkeypatch):
    p, iface = plugin
    # (quicker than for a person: the screen looked at, and the next step, at once)
    monkeypatch.setattr(tutorial_guide, "NEXT_MS", 30)
    monkeypatch.setattr(tutorial_guide, "TICK_MS", 40)
    iface.window.resize(1500, 950)
    training = _feature(p)
    assert p.start_training()
    assert wait_for(lambda: not p.context.loading)
    # The docks put away, as in a QGIS just opened: each course brings out its own.
    for dock in p.docks.values():
        dock.hide()
    yield p, iface, training
    training.end_training()


def run_through(tutorial, timeout: int = 30000) -> list:
    """The course run by its 「やって見せる」 to its end; each step's number and how it was passed."""
    passed = []

    def answer() -> None:
        # (a question QGIS asks blocks here: its はい pressed by the step that says so, as a person would)
        if tutorial.step is not None and question_yes() is not None and tutorial.step.number == "3-2":
            tutorial.show_how()

    helper = QTimer()
    helper.timeout.connect(answer)
    helper.start(100)
    try:
        while not tutorial.finished:
            index, step = tutorial.index, tutorial.step
            if tutorial.state == "skip":
                passed.append((step.number, "skip", tutorial.guide.state.text()))
                tutorial.forward()
                continue
            if step.done is None:
                if step.demo is not None:
                    tutorial.show_how()
                    wait_for(lambda: False, 300)
                passed.append((step.number, "read", ""))
                tutorial.forward()
                continue
            assert wait_for(tutorial.ready), f"{step.number} {step.title}: not ready"
            tutorial.show_how()
            moved = wait_for(lambda: tutorial.index != index or tutorial.finished, timeout)
            assert moved, f"{step.number} {step.title}: {tutorial.guide.state.text()}"
            passed.append((step.number, "done", ""))
    finally:
        helper.stop()
    return passed


def test_the_courses_mirror_the_help(practising):
    p, _iface, training = practising
    found = courses(tutorial_courses_place(training))
    assert {c.key: len(c.steps) for c in found} == COURSES
    obs = next(c for c in found if c.key == "obs")
    assert [s.number for s in obs.steps] == ["1-1", "1-2", "1-3", "1-4", "2-1", "2-2", "3-1", "4-1", "5-1", "6-1", "6-2", "6-3",
                                              "7-1", "7-2", "7-3", "8-1", "9-1", "9-2", "9-3"]
    request = next(c for c in found if c.key == "request")
    assert [s.number.split("-")[0] for s in request.steps] == ["1"] * 3 + ["2"] * 6 + ["3"] + ["5"] * 3 + ["6"] * 2 + ["7"]
    # Each step's title is the help's (040-howto.html).
    import os
    help_text = open(os.path.join(os.path.dirname(tutorial_courses.__file__), "..", "help", "src", "040-howto.html"), encoding="utf-8").read()
    for course in found:
        if course.key == "first":
            continue
        for s in course.steps:
            assert f"{s.number}. " in help_text, (course.key, s.number)


def tutorial_courses_place(training):
    from mikage.features.training import TutorialPlace

    return TutorialPlace(training)


@pytest.mark.parametrize("key", list(COURSES))
def test_every_course_runs_to_its_end_by_its_demos(practising, key):
    p, _iface, training = practising
    course = next(c for c in courses(tutorial_courses_place(training)) if c.key == key)
    tutorial = training.run_course(course)
    assert tutorial is not None and tutorial.guide.isVisible()
    # The course's role is used while it runs.
    if course.role:
        assert course.role in p.role_keeper.codes
    passed = run_through(tutorial)
    assert [n for n, _how, _why in passed] == [s.number for s in course.steps]
    skipped = [(n, why) for n, how, why in passed if how == "skip"]
    assert skipped == [], skipped
    assert tutorial.finished and "終えました" in tutorial.guide.title.text()
    progress = read_progress(training_io.active().files.progress)
    assert progress[key]["done"] is True
    if key == "obs":
        obs = p.api.observation_dock
        assert "完了" in obs.assignment_bar.label.text()
    if key == "request":
        assert any(r.name == tutorial_courses.REQUEST_NAME for r in p.api.requests.requests)
    if key == "layer":
        names = {r.name for r in p.api.requests.requests}
        assert {name for name, _no, _ring in tutorial_courses.PLACES} <= names
    if key == "plan":
        rows = p.api.schedule.rows
        assert any(r.adoption == "採用" for r in rows) and any(r.adoption_source == "手入力" for r in rows)
    # Closed (×): put aside, the frame gone.
    tutorial.guide.close()
    assert training.tutorial is None and not tutorial.frame.isVisible()


def test_the_course_goes_on_where_it_was_and_ends_with_the_practice(practising, monkeypatch):
    p, _iface, training = practising
    picked = []

    def choose(self):
        picked.append([self.tree.topLevelItem(i).text(3) for i in range(self.tree.topLevelItemCount())])
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._go(False)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(tutorial_guide.CourseList, "exec", choose)
    assert training.open_tutorial()
    tutorial = training.tutorial
    assert tutorial.course.key == "first" and tutorial.index == 0
    tutorial.forward()
    tutorial.forward()
    assert tutorial.index == 2
    # Put aside, and on from there the next time.
    tutorial.guide.close()
    assert training.tutorial is None
    assert read_progress(training_io.active().files.progress)["first"]["step"] == 2
    assert training.open_tutorial()
    assert training.tutorial.index == 2 and "手順 3 まで" in picked[-1][0]
    # 練習モード ending puts it aside too.
    training.end_training()
    assert training.tutorial is None
    assert training_io.active() is None


def test_the_tutorial_starts_the_practice_first(plugin, monkeypatch):
    p, _iface = plugin
    training = _feature(p)
    asked = []
    from mikage.ui import training_mode

    monkeypatch.setattr(training_mode.StartDialog, "exec", lambda self: asked.append(self.tutorial.isChecked()) or QDialog.DialogCode.Accepted)
    offered = []
    monkeypatch.setattr(tutorial_guide.CourseList, "exec", lambda self: offered.append(self.tree.topLevelItemCount()) or QDialog.DialogCode.Rejected)
    assert training_io.active() is None
    assert training.open_tutorial()
    assert asked == [True] and training_io.active() is not None
    # The courses offered once it is on.
    assert wait_for(lambda: offered, 5000) and offered == [len(COURSES)]
    assert QgsProject.instance().mapLayersByName("練習用の背景地図")
    training.end_training()
