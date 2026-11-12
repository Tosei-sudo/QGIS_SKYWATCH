"""「データ分析」 in QGIS: Esri tables and project layers joined and counted, the builder, analyses kept in the project, results as layers."""
import json
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer, QgsWkbTypes  # noqa: E402

from mikage.core.analysis import PROJECT_KEY, PROJECT_SCOPE, Builder, Figure  # noqa: E402
from mikage.core.esri import EsriClient  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.qgis_io.context import PluginContext  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

IMAGES = "https://example.com/arcgis/rest/services/Images/FeatureServer/0"
AI = "https://example.com/arcgis/rest/services/AI/FeatureServer/0"
S, D, T = "esriFieldTypeString", "esriFieldTypeDouble", "esriFieldTypeDate"
DAY = 86400000


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")
    settings._set("local_gpkg", str(tmp_path / "local.gpkg"))
    config = {
        "imageCatalogs": [{"label": "衛星画像", "url": IMAGES, "fields": {"id": "IMG_ID", "acquired": "ACQ", "sensor": "SENSOR", "source": "PATH"}}],
        "aiResultSources": [{"label": "船舶検出", "url": AI, "fields": {"imageId": "IMAGE_ID", "class": "CLS", "score": "SCORE", "model": "MODEL"}}],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    settings.set_config_source(str(path))
    fake = FakeEsri()
    images = fake.add_layer(IMAGES, FakeLayer({"IMG_ID": S, "ACQ": T, "SENSOR": S, "PATH": S}, "esriGeometryPolygon"))
    now = time.time() * 1000
    for i, x in enumerate((140.0, 140.2, 140.4)):
        images.add({"IMG_ID": f"IMG{i}", "ACQ": now - (i + 1) * DAY, "SENSOR": "WV03", "PATH": None},
                   {"rings": [[[x, 37.0], [x, 37.1], [x + 0.1, 37.1], [x + 0.1, 37.0], [x, 37.0]]]})
    ai = fake.add_layer(AI, FakeLayer({"IMAGE_ID": S, "CLS": S, "SCORE": D, "MODEL": S}, "esriGeometryPoint"))
    for image, cls, score, model in [("IMG0", "ship", 0.9, "yolo"), ("IMG0", "ship", 0.4, "yolo"), ("IMG0", "car", 0.8, "yolo"),
                                     ("IMG1", "ship", 0.7, "yolo"), ("IMG1", "ship", 0.9, "detr"), ("IMG2", "ship", 0.9, "detr")]:
        ai.add({"IMAGE_ID": image, "CLS": cls, "SCORE": score, "MODEL": model}, {"x": 140.05, "y": 37.05})
    from mikage import classFactory

    iface = FakeIface()
    p = classFactory(iface)
    monkeypatch.setattr(PluginContext, "esri", lambda self: EsriClient(fake))
    p.initGui()
    for dock in p.docks.values():
        dock.hide()
    assert p.api.problems == [], p.api.problems
    yield p, iface, fake, tmp_path
    p.unload()
    QgsProject.instance().clear()
    QgsSettings().remove("mikage")


def ports_layer():
    """A layer of the project: ports, with the image each one was checked on."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=port:string&field=image:string&field=berths:integer", "港 一覧", "memory")
    features = []
    for port, image, berths, x in [("A港", "IMG0", 4, 140.05), ("B港", "IMG1", 2, 140.25), ("C港", None, 1, 141.0)]:
        f = QgsFeature(layer.fields())
        f.setAttributes([port, image, berths])
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({x} 37.05)"))
        features.append(f)
    layer.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(layer)
    return layer


def ran(dock):
    out = {}
    dock.run(lambda r: out.setdefault("r", r))
    assert wait_for(lambda: "r" in out and dock.task is None, 20)
    return out["r"]


def test_ai_results_of_one_model_counted_per_image_and_class(plugin):
    p, _iface, _fake, _tmp = plugin
    ports_layer()
    dock = p.api.analysis
    dock.show()
    assert [t.name for t in dock.tables] == ["TBL_IMAGE_CATALOG", "TBL_AI_RESULT", "港_一覧"]
    # The Esri tables' columns are read when the dock opens (the layer's are known at once).
    assert wait_for(lambda: dock._columns_task is None and all(t.columns for t in dock.tables))
    ai = dock.table("TBL_AI_RESULT")
    assert [c.name for c in ai.columns][-1] == "SHAPE" and {c.role for c in ai.columns} >= {"imageId", "model"}

    # The first example: one model's results with the image's time, per image and class.
    examples = [dock.chooser.itemText(i) for i in range(dock.chooser.count())]
    assert "例: 外部 AI の結果（船舶検出）: モデルを指定して画像・クラスごとの件数" in examples
    assert dock.load("例: 外部 AI の結果（船舶検出）: モデルを指定して画像・クラスごとの件数")
    assert list(dock.param_edits) == ["model"]
    assert ran(dock) is None and "値を入力: :model" in dock.status.text()
    dock.set_value("model", "yolo")
    r = ran(dock)
    assert r.columns[0] == "IMAGE_ID" and r.columns[-1] == "件数"
    assert r.columns == ["IMAGE_ID", "ACQ", "CLS", "件数"]
    assert [(x[0], x[2], x[3]) for x in r.rows] in ([("IMG0", "ship", 2), ("IMG0", "car", 1), ("IMG1", "ship", 1)],
                                                    [("IMG0", "ship", 2), ("IMG1", "ship", 1), ("IMG0", "car", 1)])
    assert len(r.rows[0][1]) == 19  # the image's time as text
    assert "TBL_AI_RESULT 6 行" in dock.status.text() and "TBL_IMAGE_CATALOG 3 行" in dock.status.text()

    # Read with a condition (on the server): the low scores are left out.
    dock.edit_condition("TBL_AI_RESULT", "SCORE >= 0.5")
    r = ran(dock)
    assert sorted((x[0], x[2], x[3]) for x in r.rows) == [("IMG0", "car", 1), ("IMG0", "ship", 1), ("IMG1", "ship", 1)]
    assert "TBL_AI_RESULT 5 行" in dock.status.text()


def test_project_layers_join_esri_tables_in_sql_and_go_back_as_layers(plugin):
    p, _iface, _fake, _tmp = plugin
    ports_layer()
    dock = p.api.analysis
    dock.show()
    dock.set_sql("SELECT p.port, p.berths, COUNT(a.IMAGE_ID) AS results, p.SHAPE\n"
                 "FROM 港_一覧 AS p LEFT JOIN TBL_AI_RESULT AS a ON a.IMAGE_ID = p.image AND a.MODEL = :model\n"
                 "GROUP BY p.port ORDER BY p.port")
    dock.set_value("model", "yolo")
    r = ran(dock)
    assert [x[:3] for x in r.rows] == [("A港", 4, 3), ("B港", 2, 1), ("C港", 1, 0)]
    assert r.rows[0][3].startswith("Point (140.05")
    layer = dock.add_layer("港ごとの件数")
    assert layer.isValid() and QgsWkbTypes.flatType(layer.wkbType()) == QgsWkbTypes.Type.MultiPoint
    assert [f.name() for f in layer.fields()] == ["port", "berths", "results"] and layer.featureCount() == 3
    assert {f["port"]: f["results"] for f in layer.getFeatures()} == {"A港": 3, "B港": 1, "C港": 0}
    # The new layer is a table of the dock too.
    assert wait_for(lambda: dock.table("港ごとの件数") is not None)

    # The layer's own condition is a QGIS expression.
    dock.edit_condition("港_一覧", '"berths" >= 2')
    r = ran(dock)
    assert [x[0] for x in r.rows] == ["A港", "B港"]
    # Without shapes: a table.
    dock.set_sql("SELECT CLS, COUNT(*) AS n FROM TBL_AI_RESULT GROUP BY CLS ORDER BY n DESC")
    r = ran(dock)
    assert r.rows == [("ship", 5), ("car", 1)]
    table = dock.add_layer()
    assert not table.isSpatial() and table.featureCount() == 2
    assert dock.csv_text().splitlines() == ["CLS,n", "ship,5", "car,1"]

    # Nothing but reading.
    dock.set_sql("DELETE FROM TBL_AI_RESULT")
    assert ran(dock) is None and "読み取り" in dock.status.text()
    dock.set_sql("SELECT 1")
    assert ran(dock) is None and "表が 1 つもない" in dock.status.text()


def test_the_builder_suggests_keys_and_analyses_stay_in_the_project(plugin):
    p, _iface, _fake, tmp_path = plugin
    dock = p.api.analysis
    dock.show()
    assert wait_for(lambda: dock._columns_task is None and dock.table("TBL_AI_RESULT").columns)
    dock.tabs.setCurrentIndex(0)
    dock.base.setCurrentIndex(dock.base.findData("TBL_AI_RESULT"))
    dock.add_join()
    row = dock.join_rows[0]
    assert row["table"].currentData() == "TBL_IMAGE_CATALOG"
    assert dock._combo_value(row["left"]) == "t0.IMAGE_ID" and dock._combo_value(row["right"]) == "IMG_ID"
    for i in range(dock.group.count()):
        item = dock.group.item(i)
        if item.data(0x0100) in ("t0.MODEL", "t1.SENSOR"):
            item.setCheckState(2)
    dock.add_figure(Figure("countDistinct", "t0.IMAGE_ID", "画像の数"))
    assert "COUNT(DISTINCT t0.\"IMAGE_ID\")" in dock.sql.toPlainText()
    r = ran(dock)
    assert r.columns == ["MODEL", "SENSOR", "画像の数"] and sorted(r.rows) == [("detr", "WV03", 2), ("yolo", "WV03", 2)]

    # Kept in the project, and read back with it.
    assert dock.save("モデル別") == "モデル別"
    text, _ok = QgsProject.instance().readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
    assert json.loads(text)[0]["name"] == "モデル別"
    path = str(tmp_path / "analysis.qgz")
    assert QgsProject.instance().write(path)
    dock.new()
    assert dock.sql.toPlainText() == "" and not dock.join_rows
    QgsProject.instance().clear()
    assert dock.analyses == []
    assert QgsProject.instance().read(path)
    assert [a.name for a in dock.analyses] == ["モデル別"]
    assert dock.load("モデル別")
    assert dock.builder().figures[0].column == "t0.IMAGE_ID" and len(dock.join_rows) == 1
    assert sorted(dock.builder().group) == ["t0.MODEL", "t1.SENSOR"]
    assert ran(dock).rows and dock.current == "モデル別"
    assert dock.delete(confirm=False) and dock.analyses == []
    b = Builder.from_json(json.loads(text)[0]["builder"])
    assert b.table == "TBL_AI_RESULT" and b.joins[0].right == "IMG_ID"


def test_saved_analyses_are_processing_tools_that_follow_the_project(plugin, tmp_path):
    from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback

    from mikage.processing_provider import analysis_algorithm

    p, _iface, _fake, _tmp = plugin
    ports_layer()
    dock = p.api.analysis
    dock.show()
    registry = QgsApplication.processingRegistry()
    ids = lambda: sorted(a.id() for a in registry.providerById("mikage").algorithms() if a.id().startswith("mikage:analysis_"))  # noqa: E731
    assert ids() == []
    dock.set_sql("SELECT p.port, COUNT(a.IMAGE_ID) AS results, p.SHAPE\n"
                 "FROM 港_一覧 AS p LEFT JOIN TBL_AI_RESULT AS a ON a.IMAGE_ID = p.image AND a.MODEL = :model\n"
                 "GROUP BY p.port ORDER BY p.port")
    dock.set_value("model", "yolo")
    dock.tabs.setCurrentIndex(1)
    assert dock.save("港ごとの AI 件数")
    algorithm_id = dock.algorithm_id()
    assert ids() == [algorithm_id] and algorithm_id == "mikage:" + analysis_algorithm.algorithm_name("港ごとの AI 件数")
    algorithm = registry.algorithmById(algorithm_id)
    assert algorithm.displayName() == "港ごとの AI 件数" and algorithm.group() == "保存した分析"
    assert [d.name() for d in algorithm.parameterDefinitions()] == ["model", "LIMIT", "OUTPUT"]
    assert algorithm.parameterDefinition("model").defaultValue() == "yolo"
    assert "FROM 港_一覧" in algorithm.shortHelpString()

    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    out = str(tmp_path / "ports.gpkg")
    results, ok = algorithm.run({"model": "detr", "OUTPUT": out}, context, QgsProcessingFeedback())
    assert ok and results["ROWS"] == 3
    layer = QgsVectorLayer(results["OUTPUT"], "out", "ogr")
    assert layer.isValid() and QgsWkbTypes.flatType(layer.wkbType()) == QgsWkbTypes.Type.MultiPoint
    assert {f["port"]: f["results"] for f in layer.getFeatures()} == {"A港": 0, "B港": 1, "C港": 0}
    # A value is needed.
    feedback = QgsProcessingFeedback()
    _r, ok = algorithm.run({"model": "", "OUTPUT": "memory:"}, context, feedback)
    assert not ok
    del layer

    # Renamed / deleted / another project: the tools follow.
    assert dock.save("別名", ask=False) and len(ids()) == 2
    assert dock.delete(confirm=False) and len(ids()) == 1
    QgsProject.instance().clear()
    assert ids() == []
