"""
「土地被覆分類」 as a Processing algorithm (the viewer's classify dialog): the
pixels of an image put in classes by their colour (the values of the chosen
bands), either automatically (k-means: give the number of classes) or from
training polygons (one class per value of an attribute; minimum distance or
maximum likelihood), into a class-number image shown in the classes'
colours, with a table of each class's pixels, area and share, and the
classes as polygons when asked.
"""
from __future__ import annotations

import math
import os

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField,
    QgsFields, QgsGeometry, QgsPalettedRasterRenderer, QgsPointXY, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterBand, QgsProcessingParameterBoolean, QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
    QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer, QgsRasterLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QColor, QIcon

from ..core.classify import MAX_CLASSES
from ..qgis_io.change_io import MAX_POLYGONS
from ..qgis_io.classify_io import ClassifyError, TrainingClass, classify_file, format_area, outline_classes
from .pansharpen_algorithm import file_of

HERE = os.path.dirname(os.path.dirname(__file__))
METHOD_KEYS = ["kmeans", "likelihood", "distance"]
METHOD_LABELS = ["自動（教師なし、k-means）: クラスの数のみ指定", "学習ポリゴン（教師あり）: 最尤法（ばらつきも考慮・推奨）",
                 "学習ポリゴン（教師あり）: 最短距離法（平均のみ考慮）"]

#: Post-processors waiting for their layer (QGIS holds them only by pointer).
_waiting: set = set()


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def class_colors(layer: QgsRasterLayer, classes: list) -> None:
    """The class numbers in their classes' colours, named, 0 (no data) left out."""
    items = [QgsPalettedRasterRenderer.Class(c + 1, QColor(*s.color), s.name) for c, s in enumerate(classes)]
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, items))
    layer.triggerRepaint()


class ClassifyLook(QgsProcessingLayerPostProcessorInterface):
    """Names the layers, and colours the class numbers, when QGIS opens them."""

    def __init__(self, name: str, classes: list = ()):
        super().__init__()
        self.name, self.classes = name, list(classes)

    def postProcessLayer(self, layer, context, feedback) -> None:  # noqa: N802 (QGIS's name)
        _waiting.discard(self)
        layer.setName(self.name)
        if self.classes and isinstance(layer, QgsRasterLayer):
            class_colors(layer, self.classes)


def training_of(source, field: str, crs: QgsCoordinateReferenceSystem, transform_context) -> list[TrainingClass]:
    """One class per value of `field` (in order of first appearance, as the viewer's), its polygons as WKT in `crs`."""
    to = QgsCoordinateTransform(source.sourceCrs(), crs, transform_context)
    classes: dict[str, TrainingClass] = {}
    for f in source.getFeatures(QgsFeatureRequest()):
        value = f[field]
        geometry = QgsGeometry(f.geometry())
        if value is None or str(value) == "" or (hasattr(value, "isNull") and value.isNull()) or geometry.isEmpty():
            continue
        if geometry.type() != QgsWkbTypes.GeometryType.PolygonGeometry:
            continue
        if crs.isValid() and source.sourceCrs().isValid():
            geometry.transform(to)
        name = str(value)
        classes.setdefault(name, TrainingClass(name)).polygons.append(geometry.asWkt())
    return list(classes.values())


class ClassifyAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    BANDS = "BANDS"
    METHOD = "METHOD"
    CLASSES = "CLASSES"
    TRAINING = "TRAINING"
    FIELD = "FIELD"
    OUTLINE = "OUTLINE"
    MIN_PIXELS = "MIN_PIXELS"
    SEED = "SEED"
    OUTPUT = "OUTPUT"
    TABLE = "TABLE"
    OUTLINES = "OUTLINES"

    def __init__(self):
        super().__init__()
        self._looks: dict = {}
        #: What was made (for scripts and tests): the ClassifyResult of the last run.
        self.result = None

    def createInstance(self) -> "ClassifyAlgorithm":  # noqa: N802 (QGIS's name)
        return ClassifyAlgorithm()

    def name(self) -> str:
        return "classify"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("土地被覆分類")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "classify.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "画像の画素を、色（バンドの値）の似たものどうしのクラスに分けて色分けし、クラスごとの面積を算出。\n\n"
            "自動（教師なし）: 画素の見本（最大 6 万画素）を k-means でクラスの数に分割。クラス 1 が最も暗い。\n"
            "学習ポリゴン（教師あり）: ポリゴンのレイヤーで見本を囲み、クラス名の属性の値ごとに 1 クラス（2 クラス以上）。"
            "最尤法はクラスごとの平均とばらつき、最短距離法は平均のみで判定。最尤法は 1 クラスにつき「使うバンドの数 + 1」画素以上の見本が必要。\n"
            "使うバンドの指定がなければ全バンドを使用。いずれかのバンドに値のない画素は分類なし（0）。\n"
            "結果はクラス番号の画像（8 ビット、0 はデータなし）と、クラスごとの画素数・面積（楕円体）・割合の表。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("分類する画像")))
        self.addParameter(QgsProcessingParameterEnum(self.METHOD, tr("分け方"), [tr(x) for x in METHOD_LABELS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.CLASSES, tr("クラスの数（自動のとき）"), QgsProcessingParameterNumber.Type.Integer,
                                                       6, False, 2, MAX_CLASSES))
        self.addParameter(QgsProcessingParameterFeatureSource(self.TRAINING, tr("学習用のレイヤー（教師ありのとき）"),
                                                              [QgsProcessing.SourceType.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterField(self.FIELD, tr("クラス名の属性"), parentLayerParameterName=self.TRAINING, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.OUTLINE, tr("クラスごとにポリゴン化"), False))
        advanced = [
            QgsProcessingParameterBand(self.BANDS, tr("使うバンド（指定なしで全バンド）"), None, self.INPUT, optional=True, allowMultiple=True),
            QgsProcessingParameterNumber(self.MIN_PIXELS, tr("最小の大きさ（画素）"), QgsProcessingParameterNumber.Type.Integer, 9, False, 1),
            QgsProcessingParameterNumber(self.SEED, tr("乱数の種（自動のとき）"), QgsProcessingParameterNumber.Type.Integer, 1, False, 0),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("分類結果")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.TABLE, tr("クラスの表"), QgsProcessing.SourceType.TypeVector,
                                                            optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTLINES, tr("クラスのポリゴン"), QgsProcessing.SourceType.TypeVectorPolygon,
                                                            optional=True, createByDefault=True))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(tr("分類する画像を開く必要あり"))
        if not file_of(layer):
            raise QgsProcessingException(tr("ファイルの画像（GDAL で開いたラスター）のみ対応"))
        self._file, self._crs, self._layer_name = file_of(layer), layer.crs(), layer.name()
        self._band_count = layer.bandCount()
        method = METHOD_KEYS[self.parameterAsEnum(parameters, self.METHOD, context)]
        self._training = []
        if method != "kmeans":
            source = self.parameterAsSource(parameters, self.TRAINING, context)
            if source is None:
                raise QgsProcessingException(tr("学習用のレイヤー（見本を囲んだポリゴン）を選択"))
            field = self.parameterAsString(parameters, self.FIELD, context)
            if not field:
                raise QgsProcessingException(tr("クラス名を入れた属性を選択"))
            self._training = training_of(source, field, self._crs, context.transformContext())
            if len(self._training) < 2:
                raise QgsProcessingException(tr("クラスが {n} つのみ（属性の値が 2 種類以上必要）").format(n=len(self._training)))
            if len(self._training) > MAX_CLASSES:
                raise QgsProcessingException(tr("クラス数 {n}（{m} まで）").format(n=len(self._training), m=MAX_CLASSES))
        return True

    def _pixel_area(self, gt: tuple, context):
        """The ground area of one pixel around grid point (x, y), m² (a square of 16 × 16 pixels measured on the ellipsoid, divided back)."""
        if not self._crs.isValid():
            return None
        measure = QgsDistanceArea()
        measure.setSourceCrs(self._crs, context.transformContext())
        measure.setEllipsoid("EPSG:7030")

        def area(x: float, y: float) -> float:
            s = 8

            def corner(dx, dy):
                return QgsPointXY(gt[0] + (x + dx) * gt[1] + (y + dy) * gt[2], gt[3] + (x + dx) * gt[4] + (y + dy) * gt[5])
            try:
                square = QgsGeometry.fromPolygonXY([[corner(-s, -s), corner(s, -s), corner(s, s), corner(-s, s), corner(-s, -s)]])
                return measure.measureArea(square) / (4 * s * s)
            except Exception:  # noqa: BLE001 (outside the CRS's area)
                return math.nan
        return area

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        from osgeo import gdal

        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        method = METHOD_KEYS[self.parameterAsEnum(parameters, self.METHOD, context)]
        bands = [b for b in self.parameterAsInts(parameters, self.BANDS, context) if b] or list(range(1, self._band_count + 1))
        ds = gdal.Open(self._file)
        gt = ds.GetGeoTransform() if ds is not None else (0, 1, 0, 0, 0, -1)
        ds = None
        try:
            result = classify_file(
                self._file, bands, out, method, classes=self.parameterAsInt(parameters, self.CLASSES, context),
                training=self._training, seed=self.parameterAsInt(parameters, self.SEED, context),
                pixel_area=self._pixel_area(gt, context),
                progress=lambda p, text: (feedback.setProgress(0.9 * p), text and feedback.setProgressText(tr(text))),
                cancelled=feedback.isCanceled,
            )
        except ClassifyError as e:
            raise QgsProcessingException(tr(str(e)))
        self.result = result
        name = f"{self._layer_name}_{'kmeans' if method == 'kmeans' else 'classes'}"[-120:]
        self._looks = {self.OUTPUT: (name, result.classes)}
        answer = {self.OUTPUT: out}
        total = sum(c.area for c in result.classes if math.isfinite(c.area))
        feedback.pushInfo(tr("{w} × {h} px を {n} クラスに分類（バンド {b}）").format(
            w=f"{result.width:,}", h=f"{result.height:,}", n=len(result.classes), b="・".join(map(str, bands))))
        for c in result.classes:
            share = f"{100 * c.area / total:.1f}%" if total and math.isfinite(c.area) else ""
            feedback.pushInfo(f"  {c.name}: {c.pixels:,} px、{format_area(c.area)} {share}".rstrip())

        fields = QgsFields()
        for field_name, kind in (("番号", QVariant.Int), ("クラス", QVariant.String), ("色", QVariant.String), ("画素数", QVariant.Int),
                                 ("面積_km2", QVariant.Double), ("割合_%", QVariant.Double)):
            fields.append(QgsField(field_name, kind))
        sink, dest = self.parameterAsSink(parameters, self.TABLE, context, fields, QgsWkbTypes.Type.NoGeometry, QgsCoordinateReferenceSystem())
        if sink is not None:
            for k, c in enumerate(result.classes):
                f = QgsFeature(fields)
                known = math.isfinite(c.area)
                f.setAttributes([k + 1, c.name, "#%02x%02x%02x" % c.color, c.pixels, round(c.area / 1e6, 6) if known else None,
                                 round(100 * c.area / total, 2) if total and known else None])
                sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            answer[self.TABLE] = dest
            self._looks[self.TABLE] = (tr("{name} の表").format(name=name), ())

        if self.parameterAsBool(parameters, self.OUTLINE, context):
            feedback.setProgressText(tr("クラスをポリゴン化中…"))
            polygons, truncated = outline_classes(out, len(result.classes), self.parameterAsInt(parameters, self.MIN_PIXELS, context))
            outline_fields = QgsFields()
            for field_name, kind in (("クラス", QVariant.String), ("番号", QVariant.Int), ("面積_m2", QVariant.Double), ("画素数", QVariant.Int)):
                outline_fields.append(QgsField(field_name, kind))
            sink, dest = self.parameterAsSink(parameters, self.OUTLINES, context, outline_fields, QgsWkbTypes.Type.Polygon, self._crs)
            if sink is not None:
                measure = QgsDistanceArea()
                measure.setSourceCrs(self._crs, context.transformContext())
                measure.setEllipsoid("EPSG:7030")
                for rings, c, pixels in polygons:
                    geometry = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring] for ring in rings])
                    f = QgsFeature(outline_fields)
                    f.setGeometry(geometry)
                    f.setAttributes([result.classes[c - 1].name, c, round(measure.measureArea(geometry), 1), pixels])
                    sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                answer[self.OUTLINES] = dest
                self._looks[self.OUTLINES] = (tr("{name} のクラス").format(name=name), ())
                feedback.pushInfo(tr("ポリゴン {n} 個").format(n=f"{len(polygons):,}"))
                if truncated:
                    feedback.pushWarning(tr("{n} 個まで").format(n=f"{MAX_POLYGONS:,}"))
        feedback.setProgress(100)
        return answer

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        for layer_id in context.layersToLoadOnCompletion():
            details = context.layerToLoadOnCompletionDetails(layer_id)
            look = self._looks.get(details.outputName)
            if look:
                post = ClassifyLook(*look)
                _waiting.add(post)
                details.setPostProcessor(post)
        return {}


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active image (else the first), and a polygon layer as the training when there is one."""
    from qgis.core import QgsVectorLayer

    files = [l for l in layers if isinstance(l, QgsRasterLayer) and file_of(l)]
    if not files:
        return {}
    answer = {ClassifyAlgorithm.INPUT: (active if active in files else files[0]).id()}
    polygons = [l for l in layers if isinstance(l, QgsVectorLayer) and l.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry]
    if active in polygons:
        polygons.insert(0, active)
    if polygons:
        answer[ClassifyAlgorithm.TRAINING] = polygons[0].id()
    return answer
