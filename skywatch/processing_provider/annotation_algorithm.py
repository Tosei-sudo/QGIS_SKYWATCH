"""
「アノテーション変換（点→矩形）」 as Processing algorithms: point annotations
made into boxes turned to the object (core.ai.annotation_boxes), the
direction and the long and short sides from the points' attributes (heading,
length, width: what observation records measured with two to four points),
else from the object's outline found round the point by Segment Anything
(SAM) or a brightness threshold; the length given (for all, or by class),
the width given, a ratio of the length, or measured.

- `skywatch:points_to_boxes`: a point layer (observation records, a
  detection layer, any) over an image → a polygon layer;
- `skywatch:dataset_points_to_boxes`: a training dataset (教師データセット,
  the JSON of 「教師データセットの作成」) → a new dataset: its pixel
  annotations (YOLO, DOTA, VOC, COCO) written as DOTA with the point-sized
  boxes turned into boxes, its vector files as GeoPackages.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
    QgsProcessingParameterFile, QgsProcessingParameterFileDestination, QgsProcessingParameterNumber, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString, QgsProcessingOutputNumber, QgsRasterLayer, QgsVectorFileWriter, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon

from ..core.ai.annotation_boxes import (
    UNKNOWN_MODES, WIDTH_MODES, Box, ConvertError, Rules, convert, dota_row, is_point_box, parse_lengths,
)
from ..core.ai.model_config import SamModelConfig
from ..core.ai.train.dataset import Dataset, DatasetError, Group, Item, Reader, load_dataset, missing_files, save_dataset
from ..core.ai.train.labels import METRES_PER_DEGREE
from ..qgis_io.ai_io import Grid
from ..qgis_io.ai_runtime import AiError
from ..qgis_io.annotation_io import WGS84, Outliner, SamOutliner, ThresholdOutliner, load_sam, metres_per_pixel
from ..qgis_io.dataset_io import CLASS_FIELDS, image_grid, open_image, vector_layer
from . import detect_algorithm

HERE = os.path.dirname(os.path.dirname(__file__))
#: How the outline round a point is found.
METHODS = ("sam", "threshold", "none")
METHOD_LABELS = ["SAM（Segment Anything）", "簡易（明るさのしきい値、モデル不要）", "画像を見ない（属性と入力値だけ）"]
WIDTH_LABELS = ["輪郭の縦横比 × 長さ", "輪郭の実測", "長さ × 比率", "入力値（m）"]
UNKNOWN_LABELS = ["外接する正方形（どの向きでも収まる）", "北（画像の上）向きの矩形", "変換しない"]
#: Attributes read when no field is chosen (case ignored).
LENGTH_FIELDS = ("length_m", "length", "長さ", "全長")
WIDTH_FIELDS = ("width_m", "width", "幅", "全幅")
HEADING_FIELDS = ("heading_deg", "heading", "向き", "方位")
#: Fields added to the output.
OUT_FIELDS = (("box_length_m", QVariant.Double), ("box_width_m", QVariant.Double), ("box_heading", QVariant.Double),
              ("box_size_from", QVariant.String), ("box_dir_from", QVariant.String))


def tr(text: str) -> str:
    return QCoreApplication.translate("Skywatch", text)


def sam_models() -> tuple[list[SamModelConfig], str]:
    """The settings file's Segment Anything models (aiModels with encoder and decoder) and where the file is."""
    models, base = detect_algorithm._models()
    return [m for m in models if isinstance(m, SamModelConfig)], base


def field_named(fields, wanted: str, names) -> int:
    """The index of `wanted` (else the first of `names`, case ignored), −1 for none."""
    if wanted:
        return fields.indexFromName(wanted)
    lower = {f.name().lower(): i for i, f in enumerate(fields)}
    return next((lower[n.lower()] for n in names if n.lower() in lower), -1)


def value(feature, index: int):
    if index < 0:
        return None
    v = feature.attribute(index)
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    return v


def metric_ring(lon: float, lat: float, box: Box) -> list[QgsPointXY]:
    """A box worked out in metres (x east, y south) round (`lon`, `lat`) as a WGS84 ring."""
    import math

    k = max(math.cos(math.radians(lat)), 1e-6)
    ring = [QgsPointXY(lon + (x - box.cx) / (METRES_PER_DEGREE * k), lat - (y - box.cy) / METRES_PER_DEGREE) for x, y in box.corners()]
    return ring + ring[:1]


def out_values(box: Optional[Box]) -> list:
    if box is None:
        return [None] * len(OUT_FIELDS)
    return [round(box.length_m, 2), round(box.width_m, 2), None if box.angle is None else round(box.angle, 1), box.size_from, box.dir_from]


class Converter:
    """Turns points into boxes on one image (or none), as the parameters say."""

    def __init__(self, rules: Rules, outliner: Optional[Outliner], grid: Optional[Grid], crs, transform_context, mpp: float):
        self.rules = rules
        self.outliner = outliner
        self.grid = grid
        self.crs = crs
        self.transform_context = transform_context
        self.mpp = mpp
        self.skipped: dict[str, int] = {}
        self.counts: dict[str, int] = {}

    def needs_outline(self, cls, length, width, heading) -> bool:
        from ..core.ai.annotation_boxes import _positive, length_for

        known_length = _positive(length) or length_for(cls, self.rules.lengths) or (self.rules.length if self.rules.length > 0 else None)
        if heading is None or not known_length:
            return True
        return not _positive(width) and self.rules.width_mode in ("aspect", "measured")

    def box(self, x: float, y: float, cls=(), length=None, width=None, heading=None) -> Optional[Box]:
        """A point (pixels of the grid, or metres round it without an image) as a box; None (counted) when left out."""
        shape = None
        if self.outliner is not None and self.needs_outline(cls, length, width, heading):
            from ..core.ai.annotation_boxes import _positive, length_for

            known = _positive(length) or length_for(cls, self.rules.lengths) or (self.rules.length if self.rules.length > 0 else None)
            mpp = self.mpp if self.mpp > 0 else 1.0
            try:
                shape = self.outliner.shape_at(x, y, known / mpp if known else None, mpp)
            except AiError:
                raise
            except Exception as e:  # noqa: BLE001 (a window that cannot be drawn)
                self.skip(f"輪郭を取れず（{e}）")
            if shape is None:
                self.count("輪郭なし")
        method = self.outliner.method if self.outliner is not None else ""
        box, why = convert(x, y, self.mpp if self.outliner is not None or self.grid is not None else 1.0, self.rules, shape, method, cls,
                           length, width, heading)
        if box is None:
            self.skip(why)
        else:
            self.count(f"向き: {box.dir_from}")
        return box

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1

    def report(self, feedback, name: str = "") -> None:
        head = f"{name}: " if name else ""
        if self.counts:
            feedback.pushInfo(tr(head + "、".join(f"{k} {v}" for k, v in sorted(self.counts.items()))))
        for why, n in sorted(self.skipped.items()):
            feedback.pushWarning(tr("{head}{why}: {n} 件").format(head=head, why=why, n=n))


def common_parameters(alg, with_fields: bool) -> None:
    """The parameters both algorithms share (method, model, size rules)."""
    alg.addParameter(QgsProcessingParameterEnum(alg.METHOD, tr("向きと輪郭の求め方"), [tr(x) for x in METHOD_LABELS], defaultValue=0))
    labels = [m.label for m in sam_models()[0]] + [tr("ファイルを指定（下の欄）")]
    alg.addParameter(QgsProcessingParameterEnum(alg.SAM_MODEL, tr("SAM のモデル（設定ファイルの aiModels）"), labels, defaultValue=0))
    alg.addParameter(QgsProcessingParameterFile(alg.ENCODER, tr("SAM のエンコーダー（.onnx、モデルの代わり）"), extension="onnx", optional=True))
    alg.addParameter(QgsProcessingParameterFile(alg.DECODER, tr("SAM のデコーダー（.onnx）"), extension="onnx", optional=True))
    alg.addParameter(QgsProcessingParameterNumber(alg.LENGTH, tr("長さ（m、0 で輪郭から測る）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0))
    alg.addParameter(QgsProcessingParameterString(alg.LENGTHS, tr("クラスごとの長さ（例 船舶=120, 車両=5。長さより優先）"), optional=True))
    alg.addParameter(QgsProcessingParameterEnum(alg.WIDTH_MODE, tr("幅の決め方"), [tr(x) for x in WIDTH_LABELS], defaultValue=0))
    alg.addParameter(QgsProcessingParameterNumber(alg.RATIO, tr("幅 / 長さの比率（「長さ × 比率」と輪郭がないとき）"),
                                                  QgsProcessingParameterNumber.Type.Double, 0.25, False, 0.01, 10))
    alg.addParameter(QgsProcessingParameterNumber(alg.WIDTH, tr("幅（m、「入力値」のとき）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0))
    alg.addParameter(QgsProcessingParameterEnum(alg.UNKNOWN, tr("向きが分からない点"), [tr(x) for x in UNKNOWN_LABELS], defaultValue=0))
    recenter = QgsProcessingParameterBoolean(alg.RECENTER, tr("枠の中心を輪郭の中心に合わせる"), True)
    recenter.setFlags(recenter.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
    alg.addParameter(recenter)


def rules_of(alg, parameters, context) -> Rules:
    try:
        lengths = parse_lengths(alg.parameterAsString(parameters, alg.LENGTHS, context))
    except ConvertError as e:
        raise QgsProcessingException(tr(str(e)))
    rules = Rules(alg.parameterAsDouble(parameters, alg.LENGTH, context), lengths,
                  WIDTH_MODES[alg.parameterAsEnum(parameters, alg.WIDTH_MODE, context)],
                  alg.parameterAsDouble(parameters, alg.WIDTH, context), alg.parameterAsDouble(parameters, alg.RATIO, context) or 0.25,
                  UNKNOWN_MODES[alg.parameterAsEnum(parameters, alg.UNKNOWN, context)], alg.parameterAsBool(parameters, alg.RECENTER, context))
    if rules.width_mode == "fixed" and not rules.width > 0:
        raise QgsProcessingException(tr("幅を「入力値」にするときは幅（m）を指定"))
    return rules


def sam_source(alg, parameters, context) -> Optional[tuple]:
    """The SAM model chosen: (label, encoder, decoder, settings, base); None when the method is not SAM."""
    if METHODS[alg.parameterAsEnum(parameters, alg.METHOD, context)] != "sam":
        return None
    encoder = alg.parameterAsFile(parameters, alg.ENCODER, context)
    decoder = alg.parameterAsFile(parameters, alg.DECODER, context)
    if encoder or decoder:
        if not (encoder and decoder):
            raise QgsProcessingException(tr("SAM はエンコーダーとデコーダーの両方を指定"))
        return os.path.basename(encoder), encoder, decoder, None, ""
    models, base = sam_models()
    index = alg.parameterAsEnum(parameters, alg.SAM_MODEL, context)
    if 0 <= index < len(models):
        m = models[index]
        return m.label, m.encoder, m.decoder, m, base
    raise QgsProcessingException(tr("SAM のモデルを選択（設定ファイルの aiModels か、エンコーダーとデコーダーの ONNX）。"
                                    "モデルがなければ「簡易（明るさのしきい値）」か「画像を見ない」"))


class AnnotationBase(QgsProcessingAlgorithm):
    METHOD = "METHOD"
    SAM_MODEL = "SAM_MODEL"
    ENCODER = "ENCODER"
    DECODER = "DECODER"
    LENGTH = "LENGTH"
    LENGTHS = "LENGTHS"
    WIDTH_MODE = "WIDTH_MODE"
    RATIO = "RATIO"
    WIDTH = "WIDTH"
    UNKNOWN = "UNKNOWN"
    RECENTER = "RECENTER"

    def __init__(self):
        super().__init__()
        self._models = None

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "ai_segment.svg"))

    def models(self, chosen, feedback):
        """The SAM encoder and decoder (loaded once per run)."""
        if chosen is None:
            return None
        if self._models is None:
            label, encoder, decoder, config, base = chosen
            try:
                self._models = load_sam(encoder, decoder, base, label, feedback.isCanceled)
            except AiError as e:
                raise QgsProcessingException(tr(str(e)))
            feedback.pushInfo(tr("SAM: {label}（{backend}）").format(label=label, backend=self._models[0].backend))
        return self._models

    def outliner(self, method: str, chosen, layer, grid, crs, transform_context, feedback) -> Optional[Outliner]:
        if method == "none" or layer is None:
            return None
        if method == "threshold":
            return ThresholdOutliner(layer, grid, crs, transform_context)
        encoder, decoder = self.models(chosen, feedback)
        return SamOutliner(layer, grid, crs, transform_context, encoder, decoder, chosen[3])


def raster_grid(layer: QgsRasterLayer) -> Grid:
    """The image's own pixels (a service without them: about 4,000 across)."""
    native, _ = image_grid(layer)
    if native.size_x > 0 and native.size_y > 0:
        return native
    extent = layer.extent()
    size = max(extent.width(), extent.height()) / 4000
    return Grid(extent.xMinimum(), extent.yMaximum(), size, size, int(extent.width() / size) + 1, int(extent.height() / size) + 1)


class PointsToBoxesAlgorithm(AnnotationBase):
    INPUT = "INPUT"
    RASTER = "RASTER"
    CLASS_FIELD = "CLASS_FIELD"
    LENGTH_FIELD = "LENGTH_FIELD"
    WIDTH_FIELD = "WIDTH_FIELD"
    HEADING_FIELD = "HEADING_FIELD"
    OUTPUT = "OUTPUT"
    COUNT = "COUNT"

    def createInstance(self) -> "PointsToBoxesAlgorithm":  # noqa: N802 (QGIS's name)
        return PointsToBoxesAlgorithm()

    def name(self) -> str:
        return "points_to_boxes"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("アノテーション変換（点→矩形）")

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "点のアノテーション（観測記録・点のレイヤー）を、物の向きに合わせた矩形（長さ × 幅）のポリゴンにする。"
            "「AI 学習」の教師や、回転矩形（OBB）の教師データに。\n\n"
            "向き: 点の属性（heading・向き。2 点で測った観測記録の向き）→ 点のまわりの物の輪郭の長辺（SAM か、明るさのしきい値）"
            "→ 分からなければ「向きが分からない点」のとおり。輪郭の長辺は前後を区別しない（0〜180°）。\n"
            "長さ: 点の属性（length_m・長さ）→ クラスごとの長さ → 長さ（m）→ 0 なら輪郭の長辺。\n"
            "幅: 点の属性（width_m・幅。3 点・4 点で測った幅）→ 「幅の決め方」（輪郭の縦横比 × 長さ／輪郭の実測／長さ × 比率／入力値）。"
            "輪郭がなければ長さ × 比率。\n"
            "SAM は設定ファイルの aiModels（encoder・decoder）か ONNX ファイル。点のまわり（物の長さの 4 倍、長さが分からなければ 200 m 四方）を"
            "1024 画素に描いて解析するので、小さな物も拡大して見る。onnxruntime が必要（Windows 版は同梱）。\n"
            "出力は元の属性に box_length_m・box_width_m・box_heading・box_size_from・box_dir_from を加えたポリゴン。点以外の地物はそのまま。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("点のアノテーション"), [QgsProcessing.SourceType.TypeVector]))
        self.addParameter(QgsProcessingParameterRasterLayer(self.RASTER, tr("画像（輪郭を求める。「画像を見ない」なら不要）"), optional=True))
        common_parameters(self, True)
        for key, label in ((self.CLASS_FIELD, "クラスのフィールド（クラスごとの長さ用。空で class・種別 など）"),
                           (self.LENGTH_FIELD, "長さのフィールド（空で length_m・長さ など）"),
                           (self.WIDTH_FIELD, "幅のフィールド（空で width_m・幅 など）"),
                           (self.HEADING_FIELD, "向きのフィールド（度、北から時計回り。空で heading・向き など）")):
            p = QgsProcessingParameterField(key, tr(label), parentLayerParameterName=self.INPUT, optional=True)
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("矩形のアノテーション"), QgsProcessing.SourceType.TypeVectorPolygon))
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, tr("変換した点")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        self._method = METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        if layer is None and self._method != "none":
            raise QgsProcessingException(tr("輪郭を求めるには画像を指定（または「画像を見ない」）"))
        self._chosen = sam_source(self, parameters, context)
        self._rules = rules_of(self, parameters, context)
        # Drawn off the main thread: a copy of the layer.
        self._layer = layer.clone() if layer is not None else None
        self._raster_crs = layer.crs() if layer is not None else None
        self._grid = raster_grid(layer) if layer is not None else None
        self._transform_context = context.transformContext()
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        try:
            return self._process(parameters, context, feedback)
        finally:
            self._layer = None
            self._models = None

    def _process(self, parameters, context, feedback) -> dict:
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        fields = QgsFields(source.fields())
        for name, kind in OUT_FIELDS:
            if fields.indexFromName(name) < 0:
                fields.append(QgsField(name, kind))
        crs = source.sourceCrs()
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.Type.Polygon, crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        src_fields = source.fields()
        idx = {k: field_named(src_fields, self.parameterAsString(parameters, key, context), names)
               for k, key, names in (("class", self.CLASS_FIELD, CLASS_FIELDS), ("length", self.LENGTH_FIELD, LENGTH_FIELDS),
                                     ("width", self.WIDTH_FIELD, WIDTH_FIELDS), ("heading", self.HEADING_FIELD, HEADING_FIELDS))}
        found = [f"{k}={src_fields.at(i).name()}" for k, i in idx.items() if i >= 0]
        if found:
            feedback.pushInfo(tr("使う属性: {f}").format(f="、".join(found)))
        mpp = metres_per_pixel(self._grid, self._raster_crs, self._transform_context) if self._grid is not None else 0.0
        outliner = self.outliner(self._method, self._chosen, self._layer, self._grid, self._raster_crs, self._transform_context, feedback)
        converter = Converter(self._rules, outliner, self._grid, self._raster_crs, self._transform_context, mpp)
        to_raster = QgsCoordinateTransform(crs, self._raster_crs, self._transform_context) if self._raster_crs is not None else None
        from_raster = QgsCoordinateTransform(self._raster_crs, crs, self._transform_context) if self._raster_crs is not None else None
        to_wgs = QgsCoordinateTransform(crs, WGS84, self._transform_context)
        from_wgs = QgsCoordinateTransform(WGS84, crs, self._transform_context)
        total = max(source.featureCount(), 1)
        converted = 0
        started = time.monotonic()
        for n, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(100 * n / total)
            g = f.geometry()
            out = QgsFeature(fields)
            attrs = list(f.attributes()) + [None] * (fields.count() - len(f.attributes()))
            if g is None or g.isEmpty():
                converter.skip("形状なし")
                continue
            if g.type() != QgsWkbTypes.GeometryType.PointGeometry:
                if g.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
                    out.setGeometry(g)
                    out.setAttributes(attrs)
                    sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                    converter.count("点以外（そのまま）")
                else:
                    converter.skip("点・ポリゴン以外")
                continue
            p = g.centroid().asPoint()
            cls = [str(value(f, idx["class"]))] if value(f, idx["class"]) is not None else []
            length, width, heading = (value(f, idx[k]) for k in ("length", "width", "heading"))
            if self._grid is not None:
                try:
                    q = to_raster.transform(p)
                except Exception:  # noqa: BLE001
                    converter.skip("画像の座標にできない")
                    continue
                x, y = self._grid.to_pixel(q.x(), q.y())
                if not (0 <= x < self._grid.width and 0 <= y < self._grid.height):
                    converter.skip("画像の外")
                    continue
                box = converter.box(x, y, cls, length, width, heading)
                if box is None:
                    continue
                ring = [from_raster.transform(QgsPointXY(*self._grid.to_map(cx, cy))) for cx, cy in box.corners()]
                ring.append(ring[0])
            else:
                box = converter.box(0.0, 0.0, cls, length, width, heading)
                if box is None:
                    continue
                w = to_wgs.transform(p)
                ring = [from_wgs.transform(q) for q in metric_ring(w.x(), w.y(), box)]
            out.setGeometry(QgsGeometry.fromPolygonXY([ring]))
            for (name, _), v in zip(OUT_FIELDS, out_values(box)):
                attrs[fields.indexFromName(name)] = v
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            converted += 1
        converter.report(feedback)
        feedback.pushInfo(tr("{n} 点を矩形に（{s:.1f} 秒{runs}）").format(
            n=converted, s=time.monotonic() - started,
            runs=tr("、画像の解析 {r} 回").format(r=outliner.runs) if outliner is not None else ""))
        feedback.setProgress(100)
        return {self.OUTPUT: dest, self.COUNT: converted}


class DatasetPointsToBoxesAlgorithm(AnnotationBase):
    DATASET = "DATASET"
    POINT_SIZE = "POINT_SIZE"
    PIXEL_SIZE = "PIXEL_SIZE"
    OUTPUT = "OUTPUT"
    COUNT = "COUNT"

    def createInstance(self) -> "DatasetPointsToBoxesAlgorithm":  # noqa: N802 (QGIS's name)
        return DatasetPointsToBoxesAlgorithm()

    def name(self) -> str:
        return "dataset_points_to_boxes"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("アノテーション変換（教師データセット）")

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "「教師データセットの作成」の JSON を読み、点のアノテーションを物の向きに合わせた矩形にした新しい教師データセットを書く。"
            "向き・長さ・幅の決め方は「アノテーション変換（点→矩形）」と同じ。\n\n"
            "点とみなす注釈: YOLO・DOTA・VOC・COCO では幅・高さが「点とみなす大きさ」以下の枠（YOLO の w=h=0 など）、ベクタでは点の地物"
            "（向き・長さ・幅の属性も使う）。それ以外の枠・ポリゴンはそのまま残す。\n"
            "出力: 保存先の JSON と、その横のフォルダー「〈名前〉_labels」。画素の注釈は画像ごとの DOTA（.txt、回転矩形）に、"
            "ベクタは GeoPackage に書く。クラス名とクラスの対応は元のまま。画像は複製しない。\n"
            "地図座標のない画像（PNG など）の長さは「画素の大きさ」で m から画素に（0 なら長さ・幅を画素で解釈）。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFile(self.DATASET, tr("教師データセット（.json）"), extension="json"))
        common_parameters(self, False)
        self.addParameter(QgsProcessingParameterNumber(self.POINT_SIZE, tr("点とみなす枠の大きさ（画素以下）"),
                                                       QgsProcessingParameterNumber.Type.Double, 2, False, 0))
        self.addParameter(QgsProcessingParameterNumber(self.PIXEL_SIZE, tr("地図座標のない画像の画素の大きさ（m、0 で長さを画素で）"),
                                                       QgsProcessingParameterNumber.Type.Double, 0, False, 0))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("変換後の教師データセット"), tr("教師データセット (*.json)")))
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, tr("変換した点")))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        self._method = METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        self._chosen = sam_source(self, parameters, context)
        self._rules = rules_of(self, parameters, context)
        self._transform_context = context.transformContext()
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        try:
            return self._process(parameters, context, feedback)
        finally:
            self._models = None

    def _process(self, parameters, context, feedback) -> dict:
        path = self.parameterAsFile(parameters, self.DATASET, context)
        target = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not target.lower().endswith(".json"):
            target += ".json"
        if os.path.abspath(target) == os.path.abspath(path):
            raise QgsProcessingException(tr("保存先は元のデータセットと別のファイルに"))
        try:
            dataset = load_dataset(path)
        except DatasetError as e:
            raise QgsProcessingException(tr(str(e)))
        folder = os.path.splitext(target)[0] + "_labels"
        os.makedirs(folder, exist_ok=True)
        self._point = self.parameterAsDouble(parameters, self.POINT_SIZE, context)
        self._pixel = self.parameterAsDouble(parameters, self.PIXEL_SIZE, context)
        missing = set(missing_files(dataset))
        if missing:
            feedback.pushWarning(tr("見つからないファイル {n} 件（その画像はそのまま残す）: {e}").format(n=len(missing), e=sorted(missing)[0]))
        out = Dataset(f"{dataset.name or os.path.splitext(os.path.basename(path))[0]}（矩形）", stats={"from": os.path.abspath(path)})
        converted = 0
        reader = Reader()
        total = max(len(dataset.items), 1)
        done = 0
        for gi, group in enumerate(dataset.groups):
            if feedback.isCanceled():
                break
            here = os.path.join(folder, f"{gi + 1:02d}-{group.format}")
            os.makedirs(here, exist_ok=True)
            if group.format == "vector":
                new, n = self._vector_group(group, here, missing, feedback)
            else:
                new, n = self._pixel_group(group, here, missing, reader, feedback, lambda: done)
            converted += n
            done += len(group.items)
            feedback.setProgress(100 * done / total)
            out.groups.append(new)
        out.stats["converted"] = converted
        save_dataset(out, target)
        feedback.pushInfo(tr("{n} 点を矩形に。保存: {p}（注釈は {f}）").format(n=converted, p=target, f=folder))
        feedback.setProgress(100)
        return {self.OUTPUT: target, self.COUNT: converted}

    def _converter(self, image_path: str, feedback):
        """A converter on one image: (converter, grid, layer) or raises DatasetError."""
        layer = open_image(image_path)
        grid, _ = image_grid(layer)
        crs = layer.crs()
        mpp = metres_per_pixel(grid, crs, self._transform_context, self._pixel)
        outliner = self.outliner(self._method, self._chosen, layer, grid, crs, self._transform_context, feedback)
        return Converter(self._rules, outliner, grid, crs, self._transform_context, mpp), grid, layer

    def _pixel_group(self, group: Group, here: str, missing: set, reader: Reader, feedback, progress) -> tuple[Group, int]:
        class_map = dict(group.class_map)
        new = Group("dota", [], [], "", class_map, group.added)
        converted = 0
        for item in group.items:
            if feedback.isCanceled():
                break
            if item.image in missing or not item.labels or item.labels in missing:
                # Background images (no annotations), and files not there, stay as they are (in their own format, so kept apart).
                new.items.append(Item(item.image, "", item.key) if not item.labels else item)
                if item.labels:
                    feedback.pushWarning(tr("{name}: ファイルがないため変換せず").format(name=os.path.basename(item.image)))
                continue
            name = os.path.basename(item.image)
            try:
                boxes = reader.boxes(group, item)
                converter, grid, layer = self._converter(item.image, feedback)
            except (DatasetError, AiError) as e:
                feedback.pushWarning(tr("{name}: {e}（変換せずそのまま）").format(name=name, e=e))
                new.items.append(item)
                continue
            rows = []
            for b in boxes:
                b = b.in_pixels(grid.width, grid.height)
                label = b.name.replace(" ", "_")
                if label != b.name and b.name not in class_map:
                    class_map[label] = b.name
                elif label != b.name:
                    class_map[label] = class_map[b.name]
                if is_point_box(b.x1, b.y1, b.x2, b.y2, self._point):
                    box = converter.box((b.x1 + b.x2) / 2, (b.y1 + b.y2) / 2, [b.name, group.kind_of(b.name)])
                    if box is not None:
                        rows.append(dota_row(box.corners(), b.name))
                        converted += 1
                else:
                    x1, x2 = sorted((b.x1, b.x2))
                    y1, y2 = sorted((b.y1, b.y2))
                    rows.append(dota_row([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], b.name))
            converter.report(feedback, name)
            del layer
            stem = os.path.splitext(name)[0]
            labels = os.path.join(here, stem + ".txt")
            k = 1
            while os.path.exists(labels):
                k += 1
                labels = os.path.join(here, f"{stem}-{k}.txt")
            with open(labels, "w", encoding="utf-8") as f:
                f.write("\n".join(rows) + ("\n" if rows else ""))
            new.items.append(Item(item.image, labels))
        new.class_map = class_map
        return new, converted

    def _vector_group(self, group: Group, here: str, missing: set, feedback) -> tuple[Group, int]:
        new = Group("vector", [], [], group.class_field, dict(group.class_map), group.added)
        converted = 0
        by_file: dict[str, list[Item]] = {}
        for item in group.items:
            by_file.setdefault(item.labels, []).append(item)
        for labels, items in by_file.items():
            if feedback.isCanceled():
                break
            if not labels or labels in missing:
                new.items += items
                continue
            try:
                layer = vector_layer(labels)
            except DatasetError as e:
                feedback.pushWarning(tr("{e}（そのまま）").format(e=e))
                new.items += items
                continue
            self.class_map = group.class_map
            target, n = self._convert_vector(layer, [i for i in items if i.image not in missing], here, feedback, group.class_field)
            converted += n
            new.items += [Item(i.image, target if i.image not in missing else i.labels, i.key) for i in items]
        return new, converted

    def _convert_vector(self, layer, items: list[Item], here: str, feedback, class_field: str) -> tuple[str, int]:
        """The vector file's points as boxes on the images they lie on, written as a GeoPackage beside the new dataset."""
        crs = layer.crs()
        src = layer.fields()
        fields = QgsFields(src)
        for name, kind in OUT_FIELDS:
            if fields.indexFromName(name) < 0:
                fields.append(QgsField(name, kind))
        idx = {"class": field_named(src, class_field, CLASS_FIELDS), "length": field_named(src, "", LENGTH_FIELDS),
               "width": field_named(src, "", WIDTH_FIELDS), "heading": field_named(src, "", HEADING_FIELDS)}
        features = list(layer.getFeatures())
        outputs: list[QgsFeature] = []
        placed: set = set()
        converted = 0
        to_wgs = QgsCoordinateTransform(crs, WGS84, self._transform_context)
        from_wgs = QgsCoordinateTransform(WGS84, crs, self._transform_context)

        def keep(f, geometry, box=None) -> None:
            out = QgsFeature(fields)
            attrs = list(f.attributes()) + [None] * (fields.count() - len(f.attributes()))
            for (name, _), v in zip(OUT_FIELDS, out_values(box)):
                attrs[fields.indexFromName(name)] = v
            out.setAttributes(attrs)
            out.setGeometry(geometry)
            outputs.append(out)

        def attributes(f):
            cls = value(f, idx["class"])
            names = [str(cls), self.class_map.get(str(cls), str(cls))] if cls is not None else []
            return names, value(f, idx["length"]), value(f, idx["width"]), value(f, idx["heading"])

        for item in items:
            if feedback.isCanceled():
                break
            try:
                converter, grid, image = self._converter(item.image, feedback)
            except (DatasetError, AiError) as e:
                feedback.pushWarning(tr("{name}: {e}").format(name=os.path.basename(item.image), e=e))
                continue
            image_crs = image.crs()
            to_image = QgsCoordinateTransform(crs, image_crs, self._transform_context)
            from_image = QgsCoordinateTransform(image_crs, crs, self._transform_context)
            for f in features:
                if f.id() in placed:
                    continue
                g = f.geometry()
                if g is None or g.isEmpty() or g.type() != QgsWkbTypes.GeometryType.PointGeometry:
                    continue
                try:
                    q = to_image.transform(g.centroid().asPoint())
                except Exception:  # noqa: BLE001
                    continue
                x, y = grid.to_pixel(q.x(), q.y())
                if not (0 <= x < grid.width and 0 <= y < grid.height):
                    continue
                placed.add(f.id())
                box = converter.box(x, y, *attributes(f))
                if box is None:
                    continue
                ring = [from_image.transform(QgsPointXY(*grid.to_map(cx, cy))) for cx, cy in box.corners()]
                keep(f, QgsGeometry.fromPolygonXY([ring + ring[:1]]), box)
                converted += 1
            converter.report(feedback, os.path.basename(item.image))
            del image
        # Polygons as they are; points on no image from their attributes and the lengths given.
        alone = Converter(self._rules, None, None, None, self._transform_context, 0.0)
        for f in features:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if g.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
                keep(f, QgsGeometry(g))
            elif g.type() == QgsWkbTypes.GeometryType.PointGeometry and f.id() not in placed:
                box = alone.box(0.0, 0.0, *attributes(f))
                if box is not None:
                    w = to_wgs.transform(g.centroid().asPoint())
                    keep(f, QgsGeometry.fromPolygonXY([[from_wgs.transform(q) for q in metric_ring(w.x(), w.y(), box)]]), box)
                    converted += 1
        alone.report(feedback, tr("画像の外の点"))
        stem = os.path.splitext(os.path.basename(layer.source().split("|")[0]))[0]
        target = os.path.join(here, stem + ".gpkg")
        k = 1
        while os.path.exists(target):
            k += 1
            target = os.path.join(here, f"{stem}-{k}.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = stem
        writer = QgsVectorFileWriter.create(target, fields, QgsWkbTypes.Type.Polygon, crs, self._transform_context, options)
        if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
            raise QgsProcessingException(tr("書き込めず: {p}（{e}）").format(p=target, e=writer.errorMessage()))
        for out in outputs:
            writer.addFeature(out)
        del writer
        return target, converted


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active point layer (else the first) and the first visible image."""
    from qgis.core import QgsVectorLayer

    points = [l for l in layers if isinstance(l, QgsVectorLayer) and l.geometryType() == QgsWkbTypes.GeometryType.PointGeometry]
    images = [l for l in layers if isinstance(l, QgsRasterLayer)]
    answer = {}
    if points:
        answer[PointsToBoxesAlgorithm.INPUT] = (active if active in points else points[0]).id()
    if images:
        answer[PointsToBoxesAlgorithm.RASTER] = (active if active in images else images[0]).id()
    return answer
