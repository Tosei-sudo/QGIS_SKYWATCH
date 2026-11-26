"""
判読の練習の問題を作る・成績 (練習モードの段階 3 の 3) in QGIS, as an instructor
(担当「判読練習の指導者」) in the production MIKAGE: an image opened from the
production's catalog, an exercise made of it — its area, its answers taken
from an experienced interpreter's observation records of the image — written
to the organisation's tables (a fake service), changed and written again,
tried in 練習モード as the people will (the try neither kept nor sent); not
while practising; and everyone's results read as a grid with the classes
taken for others.
"""
import json
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.training import tables  # noqa: E402
from mikage.core.training.api import DEFAULT_SCORING, FIND, PRACTICE, read_results, sample_exercises  # noqa: E402
from mikage.qgis_io import settings, training_io  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.network import qgis_transport  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_exercises import answer  # noqa: E402
from mikage.tests.test_qgis_org_exercises import _types, CATALOG, ORG, ORG_CONFIG  # noqa: E402
from mikage.tests.test_qgis_training import _feature, made, plugin, PRODUCTION  # noqa: E402,F401 (the fixtures)
from mikage.ui import exercise_authoring  # noqa: E402
from mikage.ui.exercise_grades import FAIL_BG, PASS_BG  # noqa: E402

IMAGE = "PRD-20261118-0142"


@pytest.fixture
def instructor(plugin, monkeypatch):
    """The production MIKAGE (not practising) of an instructor, the organisation's tables and catalog on a fake service."""
    p, iface = plugin
    iface.window.resize(1500, 950)
    fake = FakeEsri()
    real = qgis_transport(settings.auth_for)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(
        lambda url, form: fake(url, form) if url.startswith((ORG, PRODUCTION)) else real(url, form)))
    layers = {
        "exercises": fake.add_layer(f"{ORG}/0", FakeLayer(_types(tables.EXERCISE_FIELDS), geometry="esriGeometryPolygon")),
        "answers": fake.add_layer(f"{ORG}/1", FakeLayer(_types(tables.ANSWER_FIELDS))),
        "results": fake.add_layer(f"{ORG}/2", FakeLayer(_types(tables.RESULT_FIELDS), geometry="")),
        "catalog": fake.add_layer(CATALOG, FakeLayer({"IMG_ID": "esriFieldTypeString", "PATH": "esriFieldTypeString"},
                                                     geometry="esriGeometryPolygon")),
    }
    p.role_keeper.choose(["INSTRUCTOR"])
    training = _feature(p)
    yield p, iface, training, fake, layers
    if training.practising:
        training.end_training()
    training._stop_authoring()


def sample_find():
    """The sample's 見つける exercise (its image and its ships: what the records are made of here)."""
    with open(training_io.files().sample_images, encoding="utf-8") as f:
        plans = json.load(f)
    return next(e for e in sample_exercises(plans, DEFAULT_SCORING) if e.id == "sample-find")


def opened_image(p, layers, e) -> None:
    """The exercise's image opened from the production's catalog (as from its list) and recorded on."""
    layers["catalog"].add({"IMG_ID": IMAGE, "PATH": os.path.join(training_io.files().server, "files", "catalog", f"{e.image_id}.tif")})
    opened = []
    p.api.image_catalog.open_from_catalogs(IMAGE, then=opened.append, missing=lambda: opened.append(None))
    assert wait_for(lambda: opened, 15000) and opened[0]
    obs = p.api.observation_dock
    assert wait_for(lambda: any(i.id == IMAGE for i in obs.book.images), 10000)
    obs.go_to(IMAGE)
    assert obs.book.current.id == IMAGE


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_an_instructor_makes_an_exercise_of_the_records_writes_it_and_tries_it(instructor, monkeypatch):
    p, _iface, training, fake, layers = instructor
    obs = p.api.observation_dock
    # The instructors' entries: with the organisation's tables, for the instructor's role.
    assert training._actions["判読の練習の問題を作る…"].isVisible() and training._actions["判読の練習の成績…"].isVisible()
    e = sample_find()
    opened_image(p, layers, e)
    # An experienced interpreter's records of the image: its ships, and one outside the exercise's area.
    for a in e.answers:
        p.api.add_observation(a.lon, a.lat, image=IMAGE, kind=a.kind, activity=a.activity or None, length=a.length, note=f"{a.number} の所見")
    xs, ys = [p_[0] for p_ in e.area[0]], [p_[1] for p_ in e.area[0]]
    p.api.add_observation(max(xs) + 0.01, max(ys) + 0.01, image=IMAGE, kind="船舶 > 貨物船")
    # 問題の一覧 (an exercise of the table there already), then a new one.
    layers["exercises"].add({"EXERCISE_ID": "EX-OLD", "TITLE": "前からの問題", "EXERCISE_TYPE": "見分ける", "IMAGE_ID": "X", "COURSE_NAME": "組織の入門",
                             "COURSE_ORDER": 1, "ACTIVE": 1})
    assert training.open_exercise_authoring()
    authoring = training.authoring
    manager = authoring.manager
    assert wait_for(lambda: manager.table.rowCount() == 1, 10000)
    assert manager.table.item(0, 2).text() == "前からの問題" and manager.status.text() == "1 問（出す 1・作りかけ 0）"
    editor = authoring.new()
    assert editor is not None and editor.isVisible()
    # The image the observation dock records on (opened from the catalog: by its id); the course of the one chosen, after it.
    assert (editor.image_id.text(), editor.image_source.text()) == (IMAGE, "")
    assert (editor.course.currentText(), editor.order.value(), editor.active.isChecked()) == ("組織の入門", 2, False)
    editor.title_edit.setText("港の船をすべて見つける")
    editor.instruction.setPlainText("範囲の中の船をすべて見つけて、種別と活動を記録してください。")
    editor.targets.setText("船舶")
    editor.set_area([[list(p_) for p_ in e.area[0]]])
    assert editor.area_label.text().startswith("描いた範囲 ")
    # Not written without its answers.
    assert not authoring.save()
    assert "正解なし" in editor.status.text()
    # The answers taken from the records in the area (the one outside left out), each as the record says.
    pickers = []

    def pick(self):
        pickers.append(self)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(exercise_authoring.RecordPicker, "exec", pick)
    authoring.import_records()
    assert wait_for(lambda: len(editor.answers) == len(e.answers), 10000), editor.status.text()
    assert pickers[0].table.rowCount() == len(e.answers)
    assert sorted(a.kind for a in editor.answers) == sorted(a.kind for a in e.answers)
    assert {a.explanation for a in editor.answers} == {f"{a.number} の所見" for a in e.answers}
    # Taken again: those already answers are offered unticked.
    authoring.import_records()
    assert wait_for(lambda: len(pickers) == 2, 10000)
    assert all(item.checkState() == Qt.CheckState.Unchecked for item in pickers[1].checks) and len(editor.answers) == len(e.answers)
    # The first answer's points and explanation changed in its row; an exercise of its own scoring.
    editor.table.item(0, exercise_authoring.C_POINTS).setText("20")
    editor.table.item(0, exercise_authoring.C_EXPLAIN).setText("甲板にコンテナが並ぶ")
    editor.rule_edits["tolerance"].setText("40")
    editor.pass_score.setValue(80)
    assert editor.answers[0].points == 20 and editor.dirty
    # 保存: the exercise's row and its answers' rows.
    assert authoring.save()
    assert wait_for(lambda: not editor.dirty, 10000), editor.status.text()
    assert "問題を保存" in editor.status.text() and "作りかけ" in editor.status.text()
    row = next(r for r in layers["exercises"].rows if r["attributes"]["EXERCISE_ID"] != "EX-OLD")
    a = row["attributes"]
    assert a["EXERCISE_ID"].startswith("EX-") and (a["TITLE"], a["EXERCISE_TYPE"], a["IMAGE_ID"], a["ACTIVE"], a["PASS_SCORE"]) == (
        "港の船をすべて見つける", FIND, IMAGE, 0, 80)
    assert json.loads(a["SCORING"]) == {"tolerance": 40} and a["COURSE_NAME"] == "組織の入門" and a["TARGET_KINDS"] == "船舶"
    assert row["geometry"]["rings"] and len(layers["answers"].rows) == len(e.answers)
    first = next(r["attributes"] for r in layers["answers"].rows if r["attributes"]["ANSWER_NO"] == 1)
    assert (first["ANSWER_POINTS"], first["EXPLANATION"]) == (20, "甲板にコンテナが並ぶ")
    assert not any("token" in form for url, form in fake.asked if url.startswith(ORG))
    assert [manager.table.item(r, 2).text() for r in range(manager.table.rowCount())] == ["前からの問題", "港の船をすべて見つける"]
    assert manager.table.item(1, 6).text() == "作りかけ" and manager.status.text() == "2 問（出す 1・作りかけ 1）"
    # Changed and written again: the last answer taken off (its row deleted), the others' rows kept; offered now.
    kept_ids = {r["attributes"]["OBJECTID"] for r in layers["answers"].rows}
    editor.table.selectRow(len(editor.answers) - 1)
    editor.remove_selected()
    editor.active.setChecked(True)
    assert authoring.save()
    assert wait_for(lambda: not editor.dirty, 10000), editor.status.text()
    ids = {r["attributes"]["OBJECTID"] for r in layers["answers"].rows}
    assert len(ids) == len(e.answers) - 1 and ids < kept_ids and row["attributes"]["ACTIVE"] == 1
    # The practice reads it as made: the organisation's exercise with those answers.
    offered = [x for x in tables.org_exercises(layers["exercises"].rows, layers["answers"].rows) if x.title == "港の船をすべて見つける"]
    assert len(offered) == 1 and len(offered[0].answers) == len(e.answers) - 1 and offered[0].pass_score == 80
    # 「練習モードで試す」: 練習モード started, the exercise solved as the people will; the try neither kept nor sent.
    path = training_io.files().exercise_results
    before = read_results(path)
    assert authoring.save(then_try=True)
    assert wait_for(lambda: training.practising, 15000)
    assert training.authoring is None and not editor.isVisible()
    assert wait_for(lambda: training.exercise_run is not None and training.exercise_run.bar is not None, 25000)
    run = training.exercise_run
    assert run.trial and run.exercise.title == "港の船をすべて見つける"
    assert wait_for(lambda: obs.active, 10000)
    assert obs.book.current.id == IMAGE
    for x in run.exercise.answers:
        answer(p, run, x)
    outcome = run.submit()
    assert outcome is not None and outcome.score == 100, [(i.answer.number, i.state) for i in outcome.items]
    assert "試しに解いた結果（" in run.review.note.text()
    assert read_results(path) == before and layers["results"].rows == []
    run.review.close()
    # While practising, not made (the production's tables are not written then).
    assert not training.open_exercise_authoring()
    assert not training._actions["判読の練習の問題を作る…"].isEnabled()


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_an_exercise_is_offered_or_put_aside_and_deleted_from_the_list(instructor, monkeypatch):
    p, _iface, training, _fake, layers = instructor
    layers["exercises"].add({"EXERCISE_ID": "EX-1", "TITLE": "港の船", "EXERCISE_TYPE": "見つける", "IMAGE_ID": "X", "COURSE_NAME": "入門",
                             "COURSE_ORDER": 1, "ACTIVE": 0}, {"rings": [[[139.6, 35.2], [139.6, 35.3], [139.7, 35.3], [139.6, 35.2]]]})
    layers["answers"].add({"EXERCISE_ID": "EX-1", "KIND": "船舶", "ANSWER_NO": 1}, {"x": 139.65, "y": 35.25})
    assert training.open_exercise_authoring()
    authoring = training.authoring
    manager = authoring.manager
    assert wait_for(lambda: manager.table.rowCount() == 1, 10000)
    assert manager.active_button.text() == "練習の一覧に出す"
    manager.active_button.click()
    assert wait_for(lambda: layers["exercises"].rows[0]["attributes"]["ACTIVE"] == 1, 10000)
    # (the list read again after the write: shown once it is read, a moment later under load)
    assert wait_for(lambda: manager.table.item(0, 6).text() == "出す", 10000)
    assert manager.active_button.text() == "作りかけに変更"
    # A copy to make another from: a new id, not written until saved, 作りかけ.
    editor = authoring.copy("組織:EX-1")
    assert editor.title_edit.text() == "港の船（コピー）" and not editor.active.isChecked() and len(editor.answers) == 1
    # (the list out of the way while editing: the map beside the editor)
    assert not manager.isVisible()
    # Closed with changes not saved: asked (here: kept open, then let go).
    editor.title_edit.setText("港の船 2")
    editor.ask_close = lambda: False
    editor.close()
    assert editor.isVisible() and authoring.editor is editor
    editor.ask_close = lambda: True
    editor.close()
    assert authoring.editor is None and len(layers["exercises"].rows) == 1 and manager.isVisible()
    # Deleted with its answers (asked first).
    monkeypatch.setattr(exercise_authoring, "ask_no", lambda *a, **k: True)
    manager.delete_button.click()
    assert wait_for(lambda: layers["exercises"].rows == [] and layers["answers"].rows == [], 10000)
    assert wait_for(lambda: manager.table.rowCount() == 0, 10000)
    manager.close()
    assert wait_for(lambda: training.authoring is None, 5000)


def detail(*pairs):
    return [{"no": i + 1, "state": "正しい" if t == g else "種別ちがい", "kind": t, "given": g} for i, (t, g) in enumerate(pairs)]


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_everyones_results_are_read_as_a_grid_with_the_classes_taken_for_others(instructor, tmp_path):
    p, _iface, training, _fake, layers = instructor
    for i, title in enumerate(("港の船", "船の種別", "確認試験")):
        layers["exercises"].add({"EXERCISE_ID": f"EX-{i + 1}", "TITLE": title, "EXERCISE_TYPE": "見つける", "IMAGE_ID": "X",
                                 "COURSE_NAME": "組織の入門", "COURSE_ORDER": i + 1, "EXERCISE_MODE": "試験" if i == 2 else "練習", "ACTIVE": 1})
    cargo, tanker = "船舶 > 貨物船", "船舶 > タンカー"
    tries = [("佐藤", "EX-1", 90, True, detail((cargo, cargo))), ("佐藤", "EX-2", 60, False, detail((cargo, tanker))),
             ("鈴木", "EX-1", 50, False, detail((cargo, tanker), (cargo, tanker))), ("鈴木", "EX-1", 80, True, detail((cargo, cargo))),
             ("田中", "EX-3", 85, True, [])]
    now = __import__("datetime").datetime.now()
    for k, (user, eid, score, passed, d) in enumerate(tries):
        when = (now.replace(microsecond=0) - __import__("datetime").timedelta(days=k)).isoformat()
        layers["results"].add(tables.result_attributes({
            "exercise": f"組織:{eid}", "exerciseId": eid, "source": "組織", "title": eid, "course": "組織の入門", "user": user, "mode": PRACTICE,
            "attempt": 1, "started": when, "finished": when, "seconds": 240, "score": score, "passed": passed, "detail": d}))
    shown = []
    assert training.open_exercise_grades(then=shown.append)
    assert wait_for(lambda: shown, 15000)
    dialog = shown[0]
    grid = dialog.grid
    assert [grid.item(r, 0).text() for r in range(grid.rowCount())] == ["佐藤", "田中", "鈴木"]
    assert [grid.horizontalHeaderItem(c).text() for c in range(grid.columnCount())] == ["解いた人", "1", "2", "3（試）", "進み具合"]
    # 佐藤: 1 passed (green), 2 not (red), 3 not tried; 鈴木's best of two.
    assert (grid.item(0, 1).text(), grid.item(0, 1).background().color().name()) == ("90", PASS_BG)
    assert (grid.item(0, 2).text(), grid.item(0, 2).background().color().name()) == ("60", FAIL_BG)
    assert grid.item(0, 3).text() == "—" and grid.item(0, 4).text() == "1 / 3" and grid.item(2, 1).text() == "80"
    dialog.best.setChecked(False)
    assert grid.item(2, 1).text() == "50"
    # 取り違え: 貨物船 answered タンカー 3 times by 2 people, with what to do.
    m = dialog.matrix
    rows = [m.verticalHeaderItem(r).text() for r in range(m.rowCount())]
    cols = [m.horizontalHeaderItem(c).text() for c in range(m.columnCount())]
    assert rows == ["貨物船"] and cols == ["貨物船", "タンカー"] and m.item(0, 1).text() == "3"
    assert "「船舶 > 貨物船」を「船舶 > タンカー」と答えたのが 3 回（2 人" in dialog.advice.text()
    # The period: the tries of the last days only (none older here), and the grid as CSV.
    dialog.period.setCurrentIndex(0)
    assert grid.rowCount() == 3
    out = tmp_path / "grades.csv"
    dialog.save_csv(str(out))
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("解いた人,港の船（点）") and "佐藤," in text
    dialog.close()
