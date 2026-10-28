"""
The AI tools in QGIS, as the viewer's test/ai.spec.ts, with its tiny
stand-in models (tests/data/ai/make-models.py) run by ONNX Runtime:
- 物体検出（AI） finds the bright cells of a white square on a black image
  and makes them polygons where the square is (YOLO and Detectron2 layouts,
  a model of the settings file, classes asked for);
- クリック抽出（AI） outlines a disc round the click (the stand-in decoder's
  mask, at low resolution over the padded input) and makes it a layer;
- ONNX Runtime is found where QGIS has it, else in the Windows zip's folder,
  else the person is told how to add it; models given by URL are
  downloaded once.
"""
import http.server
import math
import os
import subprocess
import sys
import threading

import numpy as np
import pytest

pytest.importorskip("qgis.core")
pytest.importorskip("onnxruntime")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings,
)

from satellite_observation.core.ai.model_config import DetectModelConfig, SamModelConfig  # noqa: E402
from satellite_observation.qgis_io import ai_runtime, settings  # noqa: E402
from satellite_observation.tests.qgis_fakes import FakeIface, wait_for  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data", "ai")
SIZE = 256
#: The white square: pixels 96–159 each way, 10 m pixels from (380000, 3950000).
SQUARE = QgsRectangle(380960, 3948400, 381600, 3949040)


def square_tiff(tmp_path) -> str:
    path = str(tmp_path / "square.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, SIZE, SIZE, 3, gdal.GDT_Byte)
    ds.SetGeoTransform([380000, 10, 0, 3950000, 0, -10])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32654)
    ds.SetProjection(srs.ExportToWkt())
    a = np.zeros((SIZE, SIZE), np.uint8)
    a[96:160, 96:160] = 255
    for b in range(3):
        ds.GetRasterBand(b + 1).WriteArray(a)
    ds = None
    return path


@pytest.fixture
def provider():
    from satellite_observation.processing_provider import detect_algorithm
    from satellite_observation.processing_provider.provider import SatObsProvider

    p = SatObsProvider()
    QgsApplication.processingRegistry().addProvider(p)
    yield p, detect_algorithm
    detect_algorithm.set_models(lambda: ([], ""))
    QgsApplication.processingRegistry().removeProvider(p)
    QgsProject.instance().clear()


def run_detect(params):
    algorithm = QgsApplication.processingRegistry().algorithmById("satobs:detect")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    results, ok = algorithm.run(dict(params, OUTPUT="memory:"), context, feedback)
    return context.takeResultLayer(results["OUTPUT"]) if ok and results.get("OUTPUT") else None, ok


def test_detection_finds_the_square_with_yolo_and_detectron2_layouts(provider, tmp_path):
    layer = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(layer)
    for name, classes in (("detect.onnx", {"bright"}), ("detectron2.onnx", {"class 0"})):
        out, ok = run_detect({"INPUT": layer.id(), "MODEL_FILE": os.path.join(DATA, name), "SCORE": 0.5})
        assert ok and out.featureCount() == 16
        assert {f["class"] for f in out.getFeatures()} == classes and min(f["score"] for f in out.getFeatures()) >= 0.5
        assert out.fields().names() == ["class", "class_id", "score", "image", "image_id", "image_time", "model", "detected_at", "note"]
        # The cells found cover the square, where it is.
        e = out.extent()
        assert [round(v) for v in (e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum())] == \
            [SQUARE.xMinimum(), SQUARE.yMinimum(), SQUARE.xMaximum(), SQUARE.yMaximum()]
        f = next(out.getFeatures())
        assert f["model"] == name and f["image"] == "square.tif" and f["detected_at"].isValid()
    # Only the classes asked for.
    out, ok = run_detect({"INPUT": layer.id(), "MODEL_FILE": os.path.join(DATA, "detect.onnx"), "SCORE": 0.5, "CLASSES": "dark"})
    assert ok and out.featureCount() == 0


def test_detection_at_a_finer_pixel_over_part_of_the_image(provider, tmp_path):
    layer = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(layer)
    # Half the pixel over the square's quarter: the cells are half as large on the ground.
    params = {"INPUT": layer.id(), "MODEL_FILE": os.path.join(DATA, "detect.onnx"), "SCORE": 0.5, "PIXEL_SIZE": 5,
              "EXTENT": "380960,381280,3948720,3949040 [EPSG:32654]"}
    out, ok = run_detect(params)
    assert ok and out.featureCount() == 16
    sizes = {round(f.geometry().boundingBox().width()) for f in out.getFeatures()}
    assert sizes == {80}


def test_a_model_of_the_settings_file_relative_to_it(provider, tmp_path):
    layer = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(layer)
    _, detect_algorithm = provider
    config = os.path.join(os.path.dirname(DATA), "config.json")
    detect_algorithm.set_models(lambda: ([DetectModelConfig("明るいセル", "./ai/detect.onnx", score=0.5),
                                          SamModelConfig("SAM", "./ai/sam-encoder.onnx", "./ai/sam-decoder.onnx")], config))
    provider[0].refreshAlgorithms()
    algorithm = QgsApplication.processingRegistry().algorithmById("satobs:detect")
    assert algorithm.parameterDefinition("MODEL").options() == ["明るいセル", "ファイルを指定（下の欄）"]
    out, ok = run_detect({"INPUT": layer.id(), "MODEL": 0})
    assert ok and out.featureCount() == 16 and next(out.getFeatures())["model"] == "明るいセル"


def test_the_dialog_starts_from_the_view(tmp_path):
    from qgis.gui import QgsMapCanvas

    from satellite_observation.processing_provider.detect_algorithm import default_inputs

    layer = QgsRasterLayer(square_tiff(tmp_path), "square")
    canvas = QgsMapCanvas()
    canvas.resize(400, 300)
    canvas.setDestinationCrs(layer.crs())
    canvas.setLayers([layer])
    canvas.setExtent(QgsRectangle(380500, 3948500, 381500, 3949500))
    inputs = default_inputs([layer], layer, canvas)
    assert inputs["INPUT"] == layer.id() and inputs["EXTENT"].endswith("[EPSG:32654]")
    assert inputs["PIXEL_SIZE"] == pytest.approx(canvas.extent().width() / canvas.width())
    assert default_inputs([], None, canvas) == {}


# クリック抽出.

@pytest.fixture
def plugin(tmp_path):
    from satellite_observation import classFactory

    QgsSettings().remove("satellite_observation")
    settings.set_config_source("")
    iface = FakeIface()
    p = classFactory(iface)
    p.initGui()
    layer = QgsRasterLayer(square_tiff(tmp_path), "square")
    QgsProject.instance().addMapLayer(layer)
    # The plugin's docks leave the map a strip of the window: widen it.
    iface.window.resize(2400, 1400)
    APP.processEvents()
    iface.canvas.setDestinationCrs(layer.crs())
    iface.canvas.setLayers([layer])
    iface.canvas.setExtent(layer.extent())
    iface.canvas.refresh()
    yield p, iface, layer
    p.unload()
    iface.setActiveLayer(None)
    QgsProject.instance().clear()
    QgsSettings().remove("satellite_observation")


def test_a_click_outlines_the_disc_round_it_and_keeps_it_as_a_layer(plugin):
    p, iface, layer = plugin
    tool = p.api.segment
    tool.action.trigger()
    assert tool.active and tool.panel.isVisible() and iface.canvas.mapTool() is tool.tool
    assert "エンコーダーとデコーダー" in tool.panel.note.text()
    # Chosen together, in either order: told apart by their names.
    tool.add_model_files([os.path.join(DATA, "sam-decoder.onnx"), os.path.join(DATA, "sam-encoder.onnx")])
    assert tool.files[0][1].endswith("sam-encoder.onnx")
    assert wait_for(tool.idle) and "準備完了（CPU）" in tool.panel.note.text()

    centre = iface.canvas.extent().center()
    tool.click(centre)
    assert wait_for(lambda: tool.idle() and tool.count() == 1, 30000), tool.panel.note.text()
    assert "1 個を抽出" in tool.panel.note.text()
    # The stand-in mask is a disc of 64 encoder pixels round the click: 64 / scale picture pixels.
    settings_ = iface.canvas.mapSettings()
    longer = max(settings_.outputSize().width(), settings_.outputSize().height())
    radius = 64 * longer / 1024 * settings_.mapUnitsPerPixel()
    [outline] = tool.outlines()
    assert 0.9 < outline.area() / (math.pi * radius * radius) < 1.1
    assert outline.centroid().asPoint().distance(centre) < 2 * settings_.mapUnitsPerPixel()

    # Shift + click: the same object (one outline still); undo takes the point back.
    tool.click(QgsPointXY(centre.x() + radius / 2, centre.y()), extend=True)
    assert wait_for(lambda: tool.idle() and len(tool.objects[0].points) == 2, 30000)
    assert tool.count() == 1
    tool.undo()
    assert wait_for(tool.idle, 30000) and len(tool.objects[0].points) == 1 and tool.count() == 1

    made = tool.keep()
    assert made.name() == "AI 抽出" and made.featureCount() == 1 and tool.count() == 0
    f = next(made.getFeatures())
    assert f["id"] == 1 and f["model"] == "sam-decoder.onnx + sam-encoder.onnx" and f["image"] == "square.tif"
    assert QgsProject.instance().layerTreeRoot().layerOrder()[0] is made
    # Another map tool puts it away.
    iface.canvas.unsetMapTool(tool.tool)
    iface.canvas.setMapTool(type(tool.tool)(iface.canvas, tool))
    assert not tool.active and not tool.panel.isVisible() and not tool.action.isChecked()


def test_a_click_without_an_image_or_a_model(plugin):
    p, iface, layer = plugin
    tool = p.api.segment
    tool.set_active(True)
    tool.click(iface.canvas.extent().center())
    assert tool.panel.note.text() == "先にモデルを選択" and not tool.objects
    iface.canvas.setLayers([])
    QgsProject.instance().removeMapLayer(layer.id())
    tool.click(iface.canvas.extent().center())
    assert tool.panel.note.text() == "画像なし（先に画像を開く）"
    tool.set_active(False)


# ONNX Runtime and the models' files.

def test_without_onnxruntime_the_person_is_told_how_to_add_it(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_runtime, "_ort", None)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(ai_runtime.RuntimeMissing, match="onnxruntime なし。OSGeo4W Shell"):
        ai_runtime.load_ort(str(tmp_path), "Windows")
    with pytest.raises(ai_runtime.RuntimeMissing, match="QGIS.app"):
        ai_runtime.load_ort(str(tmp_path), "Darwin")
    # The Windows zip's copy is for one Python only.
    (tmp_path / "onnxruntime").mkdir()
    monkeypatch.setattr(ai_runtime, "VENDORED_PYTHON", (3, 99))
    with pytest.raises(ai_runtime.RuntimeMissing, match="同梱の onnxruntime は Python 3.99 用"):
        ai_runtime.load_ort(str(tmp_path), "Windows")


def test_the_carried_onnxruntime_runs_without_its_extras(tmp_path):
    """The package as scripts/build_zip.py --win64 lays it out (its core only), found when Python has none of its own."""
    import onnxruntime
    import shutil

    from scripts.build_zip import ONNXRUNTIME_KEEP

    source = os.path.dirname(onnxruntime.__file__)
    target = tmp_path / "win64" / "onnxruntime"
    target.mkdir(parents=True)
    for name in os.listdir(source):
        if name not in ONNXRUNTIME_KEEP:
            continue
        if os.path.isdir(os.path.join(source, name)):
            shutil.copytree(os.path.join(source, name), target / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(os.path.join(source, name), target / name)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    code = (
        "import sys; sys.path = [p for p in sys.path if not os.path.isdir(os.path.join(p, 'onnxruntime'))]\n"
        "from satellite_observation.qgis_io import ai_runtime as r\n"
        f"ort = r.load_ort({str(tmp_path / 'win64')!r}, 'Windows')\n"
        "assert ort.__file__.startswith(" + repr(str(tmp_path)) + "), ort.__file__\n"
        f"m = r.create_model({os.path.join(DATA, 'detect.onnx')!r})\n"
        "import numpy as np; out = m.run({'images': np.zeros((1, 3, 64, 64), np.float32)})\n"
        "print(m.metadata['task'], list(out.values())[0].shape)\n"
    )
    result = subprocess.run([sys.executable, "-c", "import os\n" + code], cwd=root, capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, PYTHONPATH=root))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "detect (1, 6, 16)"


def test_paths_are_relative_to_the_settings_file():
    r = ai_runtime.resolve_source
    assert r("./models/a.onnx", "/srv/viewer/config.json") == os.path.normpath("/srv/viewer/models/a.onnx")
    assert r("models/a.onnx", "https://maps.example/viewer/config.json") == "https://maps.example/viewer/models/a.onnx"
    assert r("//cdn.example/a.onnx", "") == "https://cdn.example/a.onnx"
    assert r("https://x.example/a.onnx", "/srv/config.json") == "https://x.example/a.onnx"
    assert r("C:\\models\\a.onnx", "/srv/config.json") == "C:\\models\\a.onnx"
    assert ai_runtime.cache_path("https://x.example/m/yolo.onnx?v=2", "/c").endswith("-yolo.onnx")


def test_a_model_by_url_is_downloaded_once(tmp_path):
    from satellite_observation.qgis_io.tasks import run_task

    body = open(os.path.join(DATA, "detect.onnx"), "rb").read()
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path != "/models/detect.onnx":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/viewer/config.json"
    got = {}
    try:
        for key, source in (("a", "../models/detect.onnx"), ("b", "../models/detect.onnx"), ("missing", "../models/none.onnx")):
            def work(task, source=source):
                return ai_runtime.local_model(source, base, folder=str(tmp_path))

            run_task("test", work, lambda r, key=key: got.__setitem__(key, r), lambda m, key=key: got.__setitem__(key, "error: " + m))
            assert wait_for(lambda key=key: key in got)
    finally:
        server.shutdown()
    assert got["a"] == got["b"] and open(got["a"], "rb").read() == body
    assert hits.count("/models/detect.onnx") == 1
    assert got["missing"].startswith("error: ") and "HTTP 404" in got["missing"]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".part")]
