"""
「観測記録をピクセル座標で書き出す」 as a Processing algorithm (tsudo
2026-11-27): the observation records of the images chosen, each as its box
in the image file's own pixels (centre x, y, length, width, angle, the four
corners), written as GeoJSON (EPSG:4326): the box's polygon placed by the
file's own georeferencing (before any 位置ずらし or クイック幾何補正), so the
raw image and the file lie on each other in any tool (qgis_io/pixel_box_io).

`mikage:records_to_pixel_boxes`; the file is added to the map when done.
"""
from __future__ import annotations

import json
import os
import re
import time

from qgis.core import (
    QgsFillSymbol, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface, QgsProcessingOutputNumber, QgsProcessingParameterDefinition, QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination, QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
    QgsProcessingParameterString, QgsRasterLayer, QgsSingleSymbolRenderer, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.ai.api import class_of, collection
from ..core.obs.api import RecordQuery
from ..qgis_io.pixel_box_io import ImageFile, placed, records_as_features
from ..qgis_io.pixel_place import cached_placement, original_placement
from ..qgis_io.shift_io import SOURCE
from ..qgis_io.tasks import error_text
from . import train_algorithm
from .detect_algorithm import image_info

HERE = os.path.dirname(os.path.dirname(__file__))
#: Records not measured (one point): drawn by the classification's 全長・全幅 (else the default), or left out.
UNMEASURED = ("sizes", "skip")
#: Post-processors waiting for their layer (QGIS keeps only a pointer).
_waiting: set = set()
#: What the last run wrote (pixel_box_io.Written), for scripts and tests (Processing runs a copy of the algorithm).
_last = None


def last_result():
    return _last


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


def box_symbol() -> QgsFillSymbol:
    """Outlines only: the image shows through."""
    return QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "250,204,21,255", "outline_width": "0.5"})


class BoxLook(QgsProcessingLayerPostProcessorInterface):
    """The written file's look on the map: its boxes as outlines."""

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        if isinstance(layer, QgsVectorLayer):
            layer.setRenderer(QgsSingleSymbolRenderer(box_symbol()))
            layer.triggerRepaint()


def geojson_path(path: str) -> str:
    """The file to write: `.geojson` added to a name without an extension."""
    return path if os.path.splitext(path)[1] else path + ".geojson"


def write_geojson(path: str, data: dict) -> None:
    """The collection written as UTF-8, one feature per line (readable, and short enough)."""
    features = data.get("features") or []
    head = {k: v for k, v in data.items() if k != "features"}
    text = json.dumps(head, ensure_ascii=False)[:-1]
    lines = ",\n".join(json.dumps(f, ensure_ascii=False) for f in features)
    text += ',\n"features": [\n' + lines + ("\n" if lines else "") + "]}\n"
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class PixelBoxAlgorithm(QgsProcessingAlgorithm):
    IMAGES = "IMAGES"
    KINDS = "KINDS"
    UNMEASURED = "UNMEASURED"
    DEFAULT_SIZE = "DEFAULT_SIZE"
    OUTPUT = "OUTPUT"
    COUNT = "COUNT"

    def __init__(self):
        super().__init__()
        self._written = ""

    def createInstance(self) -> "PixelBoxAlgorithm":  # noqa: N802 (QGIS's name)
        return PixelBoxAlgorithm()

    def name(self) -> str:
        return "records_to_pixel_boxes"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("観測記録をピクセル座標で書き出す")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "ai_pixel_boxes.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "選んだ画像の観測記録を、画像ファイルの画素での回転矩形（中心 X・Y、長さ、幅、角度、4 隅）として GeoJSON（EPSG:4326）に書き出す。"
            "AI の学習用。\n\n"
            "画素: 画像ファイルのフル解像度の画素。左上が原点、x は右、y は下。記録時に残した画素（PIXEL_X・PIXEL_Y）を使うので、"
            "位置ずらし・クイック幾何補正をした画像でも生画像の画素。残していない古い記録は、いまの画像の上の経緯度から求める"
            "（属性 position_from）。\n"
            "長さ・幅・角度: 記録の長さ・向き・幅（2〜4 点入力）の地上の矩形を画像の画素に移し、長さの向きにそろえた矩形に。"
            "角度は前（後端→前端）の向きで、画像の上から時計回りの度（0〜360）。向きの分からない記録は角度なし、画像の軸にそろえた枠。"
            "長さを測っていない記録は分類体系の全長・全幅（なければ既定の大きさ）で書くか、書かない。\n"
            "ポリゴン: 画素の 4 隅を、画像ファイル自身の位置情報（位置ずらし・補正の前）で経緯度にしたもの。"
            "生画像を開けばそのまま重なる（位置ずらし中の画像とはずれる）。\n"
            "書かない記録: 消失・不可・数量 2 以上・削除済み。確認範囲は書かない。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterMultipleLayers(self.IMAGES, tr("画像（開いている画像。複数可）"),
                                                               QgsProcessing.SourceType.TypeRaster))
        self.addParameter(QgsProcessingParameterString(self.KINDS, tr("対象の種別（カンマ区切り、空ならすべて）"), optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.UNMEASURED, tr("長さを測っていない記録"),
                                                     [tr("分類体系の全長・全幅で書く（なければ既定の大きさ）"), tr("書かない")], defaultValue=0))
        size = QgsProcessingParameterNumber(self.DEFAULT_SIZE, tr("大きさの分からない物体の枠（m）"), QgsProcessingParameterNumber.Type.Double,
                                            20, False, 0.1)
        size.setFlags(size.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(size)
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("書き出す GeoJSON"), tr("GeoJSON (*.geojson *.json)")))
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, tr("書き出した記録")))

    # ---- on the main thread ----

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layers = [l for l in self.parameterAsLayerList(parameters, self.IMAGES, context) if isinstance(l, QgsRasterLayer)]
        if not layers:
            raise QgsProcessingException(tr("画像を指定"))
        self._files: list[ImageFile] = []
        self._notes: list[str] = []
        for layer in layers:
            name, image_id, _ = image_info(layer)
            now = cached_placement(layer, context.project())
            if not image_id or now is None:
                self._notes.append(tr("画素の位置を求められない画像（除外）: {name}").format(name=layer.name()))
                continue
            raw = placed(original_placement(layer, context.project()))
            moved = bool(layer.customProperty(SOURCE))
            self._files.append(ImageFile(image_id, name, now, raw, moved))
            if raw is None:
                self._notes.append(tr("位置情報のない画像（ポリゴンなし、画素だけ書く）: {name}").format(name=name))
            elif moved:
                self._notes.append(tr("位置ずらし・補正中の画像: ポリゴンは生画像の位置（ずらす前）に置く: {name}").format(name=name))
        if not self._files:
            raise QgsProcessingException(tr("画素の位置を求められる画像がない（GDAL で開く画像を指定）"))
        book = train_algorithm.current_book()
        if book is None:
            raise QgsProcessingException(tr("観測記録が開かれていない（観測記録パネルで保存先を選択）"))
        self._taxonomy = list(book.taxonomy())
        ids = tuple(dict.fromkeys(f.image_id for f in self._files))
        if getattr(book, "scoped", False):
            # A shared store: the images' records are asked of it in the run (off the main thread).
            self._book, self._records = book, None
        else:
            self._book, self._records = None, [o for o in book.records if o.image_id in ids]
        self._ids = ids
        kinds = [k.strip() for k in re.split(r"[,、，]", self.parameterAsString(parameters, self.KINDS, context) or "") if k.strip()]
        self._kinds = kinds
        self._written = ""
        return True

    # ---- in the algorithm's thread ----

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        started = time.monotonic()
        records = self._records
        if records is None:
            try:
                records = self._book.fetch(RecordQuery(image_ids=self._ids))
            except Exception as error:  # noqa: BLE001
                raise QgsProcessingException(tr("観測記録の取得に失敗: {e}").format(e=error_text(error)))
        taxonomy, kinds = self._taxonomy, self._kinds
        wanted = (lambda kind: class_of(kind, taxonomy, 0, kinds) is not None) if kinds else None
        measured_only = UNMEASURED[self.parameterAsEnum(parameters, self.UNMEASURED, context)] == "skip"
        default_size = self.parameterAsDouble(parameters, self.DEFAULT_SIZE, context) or 20.0
        written = records_as_features(records, self._files, taxonomy, wanted, default_size, measured_only)
        global _last
        _last = written
        path = geojson_path(self.parameterAsFileOutput(parameters, self.OUTPUT, context))
        images = [f.info() for f in self._files]
        try:
            write_geojson(path, collection(written.features, images, os.path.splitext(os.path.basename(path))[0]))
        except OSError as error:
            raise QgsProcessingException(tr("書き出しに失敗: {e}").format(e=error))
        for note in self._notes:
            feedback.pushInfo(note)
        for why, n in sorted(written.skipped.items()):
            feedback.pushInfo(tr("書かない記録: {why} {n} 件").format(why=why, n=n))
        if written.positions:
            feedback.pushInfo(tr("位置の出どころ: {t}").format(t="、".join(f"{k} {n}" for k, n in written.positions.items())))
        if written.sizes:
            feedback.pushInfo(tr("大きさの出どころ: {t}").format(t="、".join(f"{k} {n}" for k, n in written.sizes.items())))
        if written.unplaced:
            feedback.pushWarning(tr("ポリゴンなし（画素だけ）: {n} 件").format(n=written.unplaced))
        feedback.pushInfo(tr("{n} 件を書き出し（画像 {m} 枚、{s:.1f} 秒）: {path}").format(
            n=len(written.features), m=len(images), s=time.monotonic() - started, path=path))
        if not written.features:
            feedback.pushWarning(tr("書き出す記録がない（画像の観測記録を確認）"))
        self._written = path
        feedback.setProgress(100)
        return {self.OUTPUT: path, self.COUNT: len(written.features)}

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        # The file on the map (a file destination is not opened by Processing itself).
        if self._written and context.project() is not None:
            details = QgsProcessingContext.LayerDetails(os.path.splitext(os.path.basename(self._written))[0], context.project(), self.OUTPUT)
            post = BoxLook()
            _waiting.add(post)
            details.setPostProcessor(post)
            context.addLayerToLoadOnCompletion(self._written, details)
        return {}


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active image (else the first)."""
    images = [l for l in layers if isinstance(l, QgsRasterLayer)]
    if not images:
        return {}
    layer = active if active in images else images[0]
    return {PixelBoxAlgorithm.IMAGES: [layer.id()]}
