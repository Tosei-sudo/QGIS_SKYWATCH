"""
判読の練習's organisation's exercises (練習モードの段階 3 の 2) in QGIS: the
exercises and answers read from the organisation's Esri tables (the settings
file's `training.exercises`), offered with the sample's, solved as a person
does and their try sent to its results table (one row; sent again later when
it could not be); an exam taken on another computer not offered again; an
image only in the production's catalog opened from there (read only), and one
given by its file.
"""
import os

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtWidgets import QDialog  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.training import tables  # noqa: E402
from mikage.core.training.api import EXAM, PRACTICE, read_results  # noqa: E402
from mikage.qgis_io import settings, training_io  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.qgis_io.network import qgis_transport  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_exercises import answer, started  # noqa: E402
from mikage.tests.test_qgis_training import _feature, made, plugin, PRODUCTION  # noqa: E402,F401 (the fixtures)
from mikage.ui import exercise_screens  # noqa: E402

ORG = "https://org.invalid/arcgis/rest/services/Training/FeatureServer"
CATALOG = f"{PRODUCTION}/Catalog/FeatureServer/0"
TOKEN = "ORG-TOKEN"
ORG_CONFIG = {
    "training": {"exercises": {"exercises": f"{ORG}/0", "answers": f"{ORG}/1", "results": f"{ORG}/2", "token": TOKEN}},
    "imageCatalogs": [{"label": "本番カタログ", "url": CATALOG, "fields": {"id": "IMG_ID", "source": "PATH"}}],
}
NUMBERS = {"COURSE_ORDER", "TIME_LIMIT_MIN", "PASS_SCORE", "ANSWER_NO", "LENGTH_M", "WIDTH_M", "HEADING", "ANSWER_POINTS", "TOLERANCE_M",
           "ATTEMPT", "SCORE", "SECONDS", "FOUND", "MISSED", "EXTRA", "KIND_SCORE", "HINTS"}
DATES = {"STARTED", "FINISHED"}


def _types(fields) -> dict:
    return {n: "esriFieldTypeDouble" if n in NUMBERS else "esriFieldTypeDate" if n in DATES else "esriFieldTypeString" for n in fields}


@pytest.fixture
def org(plugin, monkeypatch):
    """練習モード started, the organisation's tables (and the production's catalog) on a fake service."""
    p, iface = plugin
    iface.window.resize(1500, 950)
    fake = FakeEsri()
    real = qgis_transport(settings.auth_for)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(
        lambda url, form: fake(url, form) if url.startswith((ORG, PRODUCTION)) else real(url, form)))
    layers = {
        "exercises": fake.add_layer(f"{ORG}/0", FakeLayer(_types(tables.EXERCISE_FIELDS), geometry="esriGeometryPolygon", token=TOKEN)),
        "answers": fake.add_layer(f"{ORG}/1", FakeLayer(_types(tables.ANSWER_FIELDS), token=TOKEN)),
        "results": fake.add_layer(f"{ORG}/2", FakeLayer(_types(tables.RESULT_FIELDS), geometry="", token=TOKEN)),
        "catalog": fake.add_layer(CATALOG, FakeLayer({"IMG_ID": "esriFieldTypeString", "PATH": "esriFieldTypeString"},
                                                     geometry="esriGeometryPolygon")),
    }
    training = _feature(p)
    assert training.settings.exercises is not None
    assert p.start_training()
    assert wait_for(lambda: not p.context.loading)
    yield p, iface, training, fake, layers
    training.end_training()


def add_exercise(layers, e, eid: str, **values) -> None:
    """A sample exercise as the organisation's rows (its id, and the columns given changed)."""
    row = {"EXERCISE_ID": eid, "TITLE": f"組織の{e.title}", "INSTRUCTION": e.instruction, "EXERCISE_TYPE": e.type, "IMAGE_ID": e.image_id,
           "BEFORE_IMAGE_ID": e.before_image_id or None, "TARGET_KINDS": ";".join(e.target_kinds), "EXERCISE_MODE": e.mode,
           "COURSE_NAME": "組織の入門", "COURSE_ORDER": len(layers["exercises"].rows) + 1, "ACTIVE": 1}
    row.update(values)
    layers["exercises"].add(row, {"rings": [[[x, y] for x, y in ring] for ring in e.area]})
    for a in e.answers:
        layers["answers"].add({"EXERCISE_ID": eid, "ANSWER_NO": a.number, "KIND": a.kind, "ACTIVITY": a.activity or None,
                               "LENGTH_M": a.length, "WIDTH_M": a.width, "HEADING": a.heading, "CHANGE_KIND": a.change or None,
                               "ANSWER_POINTS": a.points, "REQUIRED": 1 if a.required else 0, "EXPLANATION": f"{a.number} 番の解説"},
                              {"x": a.lon, "y": a.lat})


def read(training) -> None:
    done = []
    assert training.read_org_exercises(then=lambda: done.append(True))
    assert wait_for(lambda: done, 15000)


def image_file(image_id: str) -> str:
    return os.path.join(training_io.active().files.server, "files", "catalog", f"{image_id}.tif")


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_an_organisations_exercise_is_offered_solved_and_its_try_sent(org, monkeypatch):
    p, _iface, training, fake, layers = org
    obs = p.api.observation_dock
    # (the user name the tables are edited by: the same on every computer)
    user = training._user()
    assert user
    sample = {e.id: e for e in training.exercises()}
    add_exercise(layers, sample["sample-find"], "ORG-FIND", DIFFICULTY="中", PASS_SCORE=80)
    add_exercise(layers, sample["sample-exam"], "ORG-EXAM", EXERCISE_MODE="試験", TIME_LIMIT_MIN=20)
    add_exercise(layers, sample["sample-identify"], "ORG-DRAFT", ACTIVE=0)
    # The exam taken on another computer: a row of the results table.
    layers["results"].add(tables.result_attributes({
        "exerciseId": "ORG-EXAM", "title": "組織の確認試験", "user": user, "mode": EXAM, "attempt": 1, "started": "2026-11-20T09:00:00",
        "finished": "2026-11-20T09:15:00", "seconds": 900, "score": 72, "passed": False, "found": 3, "missed": 1}))
    layers["results"].add(tables.result_attributes({"exerciseId": "ORG-FIND", "user": "ほかの人", "mode": PRACTICE, "score": 10}))
    dialogs = []

    def choose(self):
        dialogs.append(self)
        self.choice = "組織:ORG-FIND"
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(exercise_screens.ExerciseList, "exec", choose)
    assert training.open_exercises()
    assert wait_for(lambda: training.exercise_run is not None and training.exercise_run.bar is not None, 20000)
    run = training.exercise_run
    assert wait_for(lambda: obs.active, 10000)
    # Offered: the organisation's 2 (the draft not), their course first, then the sample's; their tables asked with their token.
    d = dialogs[0]
    titles = [d.table.item(r, 1).text() for r in range(d.table.rowCount())]
    assert titles == ["組織の船を見つける", "組織の確認試験（船の判読・入門）"], titles
    assert d.course.count() == 3 and d.course.itemText(0) == "組織の入門" and len(d.exercises) == 8
    assert d.source.text() == "組織の問題 2 問・見本の問題（横須賀）6 問"
    assert d.table.item(1, 7).text() == "済（1 回だけ）"
    asked = [form for url, form in fake.asked if url.startswith(ORG)]
    assert asked and all(form.get("token") == TOKEN for form in asked)
    # (only this person's tries read from the results table)
    assert [r["exerciseId"] for r in training.org_results] == ["ORG-EXAM"]
    e = run.exercise
    assert (e.key, e.source, e.pass_score, e.difficulty) == ("組織:ORG-FIND", "組織", 80, "中")
    assert obs.book.current.id == e.image_id and e.answers[0].explanation == "1 番の解説"
    for a in e.answers:
        answer(p, run, a)
    outcome = run.submit()
    assert outcome is not None and outcome.score == 100
    # One row in the results table, the try marked sent on this computer.
    assert wait_for(lambda: len(layers["results"].rows) == 3, 10000)
    row = layers["results"].rows[-1]["attributes"]
    assert (row["EXERCISE_ID"], row["USER_NAME"], row["EXERCISE_MODE"], row["ATTEMPT"], row["SCORE"], row["PASSED"]) == ("ORG-FIND", user, PRACTICE, 1, 100, 1)
    assert row["COURSE_NAME"] == "組織の入門" and row["FOUND"] == len(e.answers)
    assert wait_for(lambda: "結果を組織の表に送信" in run.review.note.text(), 10000)
    kept = read_results(training_io.active().files.exercise_results)
    assert kept[-1]["exercise"] == "組織:ORG-FIND" and kept[-1]["sent"] is True
    run.review.close()
    # The exam taken elsewhere is not started (from the list, or as the one after).
    exam = next(x for x in training.exercises() if x.id == "ORG-EXAM")
    assert training.run_exercise(exam) is None and training.exercise_run is None
    # Read again: the try counted once (this computer's and the table's), the next one is the 2nd.
    read(training)
    counted = [r for r in training.all_results() if r.get("exercise") == "組織:ORG-FIND"]
    assert len(counted) == 1


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_a_try_that_could_not_be_sent_is_sent_next_time(org, monkeypatch):
    p, _iface, training, fake, layers = org
    sample = {e.id: e for e in training.exercises()}
    add_exercise(layers, sample["sample-identify"], "ORG-IDENTIFY")
    read(training)
    run = started(training, "ORG-IDENTIFY")
    for a in run.exercise.answers:
        answer(p, run, a)
    # The results table not reachable now.
    results = fake.layers.pop(f"{ORG}/2")
    assert run.submit() is not None
    assert wait_for(lambda: "組織の表への送信に失敗" in run.review.note.text(), 10000)
    path = training_io.active().files.exercise_results
    assert read_results(path)[-1]["sent"] is False
    run.review.close()
    # Reachable again: sent when the exercises are read (「判読の練習…」 opened) next.
    fake.add_layer(f"{ORG}/2", results)
    read(training)
    assert len(results.rows) == 1 and results.rows[0]["attributes"]["EXERCISE_ID"] == "ORG-IDENTIFY"
    assert read_results(path)[-1]["sent"] is True
    read(training)
    assert len(results.rows) == 1


@pytest.mark.parametrize("plugin", [ORG_CONFIG], indirect=True)
def test_an_image_only_in_the_production_catalog_or_given_by_its_file_is_opened(org):
    p, _iface, training, fake, layers = org
    obs = p.api.observation_dock
    sample = {e.id: e for e in training.exercises()}
    e = sample["sample-find"]
    add_exercise(layers, e, "ORG-PROD", IMAGE_ID="PROD-0136")
    layers["catalog"].add({"IMG_ID": "PROD-0136", "PATH": image_file(e.image_id)})
    add_exercise(layers, e, "ORG-FILE", IMAGE_ID=None, IMAGE_SOURCE=image_file(e.image_id))
    read(training)
    for key in ("ORG-PROD", "ORG-FILE"):
        run = started(training, key)
        if key == "ORG-PROD":
            # (looked up in the production's catalog, read only)
            assert any(url == f"{CATALOG}/query" for url, _form in fake.asked)
            assert obs.book.current.id == "PROD-0136"
        else:
            assert run.exercise.image_source and obs.book.current is not None and obs.book.current.layer_ids
        for a in run.exercise.answers:
            answer(p, run, a)
        outcome = run.submit()
        assert outcome is not None and outcome.score == 100, (key, [(i.answer.number, i.state) for i in outcome.items])
        run.review.close()
        assert training.exercise_run is None
    # Nothing written to the production's catalog.
    assert not [url for url, _form in fake.asked if url.startswith(CATALOG) and not url.endswith(("/query", "/0"))]
