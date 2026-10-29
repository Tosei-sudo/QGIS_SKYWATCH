"""
「AI 学習（物体検出）」 as a Processing algorithm: a detection model trained
in QGIS on images and their labelled objects, written as an ONNX file that
「物体検出（AI）」 runs (and lists as 「学習済み」).

The labels are the observation records of each image (the boxes from their
length and heading, else the classification's 全長・全幅), with the image's
checked areas as places where everything is labelled; or the features of a
vector layer (points or polygons) and a class field. Classes are kinds of
the classification (分類体系), cut to a depth if asked, so what is found
can go straight back into the observation book.

The input is the image as shown (RGB) or the values of chosen bands: one
band (単バンド) or several (マルチバンド). A model trained before can be
trained further (追加学習), new classes added to it.

Training runs on NumPy alone (see core/ai/train): nothing to install, in
the normal zip as in the Windows one.
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable

import numpy as np
from qgis.core import (
    QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessing,
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
    QgsProcessingParameterFile, QgsProcessingParameterFileDestination, QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
    QgsProcessingParameterString, QgsRasterLayer, QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon

from ..core.ai.train.labels import class_order
from ..core.ai.train.trainer import (
    TILE_SIZES, InputSpec, TrainError, TrainOptions, add_classes, band_statistics, history_entry, load_model, new_model, normalize, split, train,
)
from ..qgis_io.ai_io import band_values, check_bands, grid_of, render_rgba
from ..qgis_io.ai_runtime import AiError
from ..qgis_io.train_io import Gathered, area_geometries, labels_from_book, labels_from_features, pixel_boxes, thin, windows_of
from .detect_algorithm import image_info

HERE = os.path.dirname(os.path.dirname(__file__))
SIZES = ("small", "medium", "large")
SOURCES = ("book", "layer")

#: The observation book of the plugin (records, areas, classification), given by the plugin.
_book: Callable[[], object] = lambda: None


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


def set_book(book: Callable[[], object]) -> None:
    """Where the algorithm finds the observation book: `() -> ObservationBook or None`."""
    global _book
    _book = book


def parse_bands(text: str) -> list[int]:
    """Band numbers from `1,2,3` / `1-4` / `4 3 2`; empty for the image as shown."""
    out: list[int] = []
    for part in re.split(r"[,、，\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-‐－〜~]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(a, b + 1) if a <= b else range(a, b - 1, -1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise QgsProcessingException(tr("バンドは番号で指定（例 1,2,3,4 / 1-4 / 単バンドなら 1）"))
    if any(b < 1 for b in out) or len(set(out)) != len(out):
        raise QgsProcessingException(tr("バンドの番号は 1 以上で重複なし"))
    return out


class TrainAlgorithm(QgsProcessingAlgorithm):
    IMAGES = "IMAGES"
    SOURCE = "SOURCE"
    LABELS = "LABELS"
    CLASS_FIELD = "CLASS_FIELD"
    KINDS = "KINDS"
    DEPTH = "DEPTH"
    BANDS = "BANDS"
    BASE_MODEL = "BASE_MODEL"
    EPOCHS = "EPOCHS"
    TILE = "TILE"
    SIZE = "SIZE"
    PIXEL_SIZE = "PIXEL_SIZE"
    DEFAULT_SIZE = "DEFAULT_SIZE"
    BATCH = "BATCH"
    LEARNING_RATE = "LEARNING_RATE"
    VALIDATION = "VALIDATION"
    REMEMBER = "REMEMBER"
    OUTPUT = "OUTPUT"
    BOXES = "BOXES"

    def createInstance(self) -> "TrainAlgorithm":  # noqa: N802 (QGIS's name)
        return TrainAlgorithm()

    def name(self) -> str:
        return "train_detector"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("AI 学習（物体検出）")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "ai_train.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "画像と、そこに記録した物体から物体検出モデルを学習し、ONNX で保存。保存したモデルは「物体検出（AI）」の"
            "「学習済み」で利用。\n\n"
            "教師データ: 観測記録なら各画像の記録（不在を除く）。枠の大きさは記録の長さ・向き、なければ分類体系の諸元（全長・全幅）、"
            "なければ既定の大きさ。その画像の確認範囲は「すべて記録済み」とみなし背景も学習。"
            "ベクタレイヤーなら点・ポリゴンとクラスのフィールド（種別、または分類の名前・別名）。image_id のある地物はその画像だけに使用。\n"
            "クラス: 分類体系の種別。深さを指定すると上位の分類にまとめて学習（例 1 で「船舶」）。対象の種別を指定するとその配下だけ。\n"
            "バンド: 空なら表示のまま（RGB）。番号を指定するとバンドの値で学習（1 つなら単バンド、例 1,2,3,4 でマルチバンド）。"
            "値は学習時の平均・標準偏差で正規化し、モデルに保存。\n"
            "追加学習: このプラグインで学習したモデルを指定すると、その重みから学習を継続。新しい種別はクラスとして追加。"
            "以前のクラスを忘れないよう、以前の画像も一緒に指定すると効果的。\n\n"
            "追加ライブラリ不要（NumPy で学習、CPU のみ）。小さな物体は画素を小さく、大きな物体は画素を大きく。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterMultipleLayers(self.IMAGES, tr("学習する画像"), QgsProcessing.SourceType.TypeRaster))
        self.addParameter(QgsProcessingParameterEnum(self.SOURCE, tr("教師データ"),
                                                     [tr("観測記録（各画像の記録と確認範囲）"), tr("ベクタレイヤー（下の欄）")], defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LABELS, tr("教師データのレイヤー（点・ポリゴン）"),
            [QgsProcessing.SourceType.TypeVectorPoint, QgsProcessing.SourceType.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterField(self.CLASS_FIELD, tr("クラスのフィールド（種別）"), parentLayerParameterName=self.LABELS,
                                                      optional=True))
        self.addParameter(QgsProcessingParameterString(self.KINDS, tr("対象の種別（カンマ区切り、空ならすべて）"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.DEPTH, tr("分類の深さ（0 で記録のまま、1 で最上位）"),
                                                       QgsProcessingParameterNumber.Type.Integer, 0, False, 0, 10))
        self.addParameter(QgsProcessingParameterString(self.BANDS, tr("バンド（空で表示のまま RGB、例 1 / 1,2,3,4）"), optional=True))
        self.addParameter(QgsProcessingParameterFile(self.BASE_MODEL, tr("追加学習の元のモデル（空で新規）"), extension="onnx", optional=True))
        self.addParameter(QgsProcessingParameterNumber(self.EPOCHS, tr("エポック数"), QgsProcessingParameterNumber.Type.Integer, 30, False, 1, 1000))
        self.addParameter(QgsProcessingParameterEnum(self.TILE, tr("入力の大きさ（画素）"), [str(t) for t in TILE_SIZES], defaultValue=1))
        self.addParameter(QgsProcessingParameterEnum(self.SIZE, tr("モデルの大きさ"), [tr("小（速い）"), tr("中"), tr("大（高精度・遅い）")],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.PIXEL_SIZE, tr("画素の大きさ（画像の単位、0 で画像の画素）"),
                                                       QgsProcessingParameterNumber.Type.Double, 0, False, 0))
        advanced = [
            QgsProcessingParameterNumber(self.DEFAULT_SIZE, tr("大きさの分からない物体の枠（m）"), QgsProcessingParameterNumber.Type.Double,
                                         20, False, 0.1),
            QgsProcessingParameterNumber(self.BATCH, tr("バッチの大きさ"), QgsProcessingParameterNumber.Type.Integer, 8, False, 1, 64),
            QgsProcessingParameterNumber(self.LEARNING_RATE, tr("学習率（0 で自動）"), QgsProcessingParameterNumber.Type.Double, 0, False, 0, 0.1),
            QgsProcessingParameterNumber(self.VALIDATION, tr("検証に使う割合（%）"), QgsProcessingParameterNumber.Type.Integer, 15, False, 0, 50),
            QgsProcessingParameterBoolean(self.REMEMBER, tr("「物体検出（AI）」のモデルに追加"), True),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("学習したモデル（ONNX）"), tr("ONNX モデル (*.onnx)")))
        self.addParameter(QgsProcessingParameterFeatureSink(self.BOXES, tr("学習に使った枠"), QgsProcessing.SourceType.TypeVectorPolygon,
                                                            optional=True, createByDefault=False))

    # ---- on the main thread ----

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        layers = [l for l in self.parameterAsLayerList(parameters, self.IMAGES, context) if isinstance(l, QgsRasterLayer)]
        if not layers:
            raise QgsProcessingException(tr("学習する画像を指定"))
        size = self.parameterAsDouble(parameters, self.PIXEL_SIZE, context)
        self._images = []
        self._layers = []
        for layer in layers:
            pixel = size if size > 0 else (layer.rasterUnitsPerPixelX() if layer.providerType() == "gdal" else 0)
            if not pixel > 0:
                pixel = max(layer.extent().width(), layer.extent().height()) / 4000
            name, image_id, _ = image_info(layer)
            # Read off the main thread: a copy of the layer, as QGIS's own map rendering algorithms do.
            self._layers.append(layer.clone())
            self._images.append({"name": name, "id": image_id, "crs": layer.crs(), "extent": layer.extent(),
                                 "pixel": pixel, "bands": layer.bandCount()})
        self._transform_context = context.transformContext()
        book = _book()
        taxonomy = list(book.taxonomy()) if book is not None else []
        self._taxonomy = taxonomy
        depth = self.parameterAsInt(parameters, self.DEPTH, context)
        kinds = [k.strip() for k in re.split(r"[,、，]", self.parameterAsString(parameters, self.KINDS, context) or "") if k.strip()]
        default_size = self.parameterAsDouble(parameters, self.DEFAULT_SIZE, context) or 20.0
        source = SOURCES[self.parameterAsEnum(parameters, self.SOURCE, context)]
        if source == "book":
            if book is None:
                raise QgsProcessingException(tr("観測記録が開かれていない（観測記録パネルで保存先を選択、またはベクタレイヤーを指定）"))
            self._gathered = labels_from_book(list(book.records), list(book.areas), taxonomy, depth, kinds, default_size)
            self._source_label = tr("観測記録（{label}）").format(label=book.label)
        else:
            features = self.parameterAsSource(parameters, self.LABELS, context)
            if features is None:
                raise QgsProcessingException(tr("教師データのレイヤーを指定"))
            field = self.parameterAsString(parameters, self.CLASS_FIELD, context)
            if not field:
                names = [f.name() for f in features.fields()]
                field = next((n for n in ("class", "KIND", "kind", "種別", "CLASS") if n in names), "")
            if not field:
                raise QgsProcessingException(tr("クラスのフィールドを指定"))
            try:
                self._gathered = labels_from_features(features.getFeatures(), features.fields(), features.sourceCrs(), self._transform_context,
                                                      field, taxonomy, depth, kinds, default_size)
            except ValueError as e:
                raise QgsProcessingException(tr(str(e)))
            self._source_label = tr("レイヤー（{field}）").format(field=field)
        self._base_path = self.parameterAsFile(parameters, self.BASE_MODEL, context)
        return True

    # ---- in the algorithm's thread ----

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        try:
            return self._process(parameters, context, feedback)
        finally:
            # The layer copies go now: a refused run's algorithm lives on in QGIS's exception until exit, after GDAL.
            self._layers = []

    def _process(self, parameters, context, feedback) -> dict:
        started = time.monotonic()
        gathered: Gathered = self._gathered
        bands = parse_bands(self.parameterAsString(parameters, self.BANDS, context))
        options = TrainOptions(
            epochs=self.parameterAsInt(parameters, self.EPOCHS, context),
            tile=TILE_SIZES[self.parameterAsEnum(parameters, self.TILE, context)],
            size=SIZES[self.parameterAsEnum(parameters, self.SIZE, context)],
            batch=self.parameterAsInt(parameters, self.BATCH, context),
            validation=self.parameterAsInt(parameters, self.VALIDATION, context) / 100,
        )
        rate = self.parameterAsDouble(parameters, self.LEARNING_RATE, context)
        for why, n in gathered.skipped.items():
            feedback.pushInfo(tr("対象外: {why} {n} 件").format(why=why, n=n))
        found = sorted({l.cls for l in gathered.labels})
        if not found:
            raise QgsProcessingException(tr("学習できる物体なし（種別のある記録・地物が必要）"))

        # The model: trained on from a file, or new.
        if self._base_path:
            try:
                with open(self._base_path, "rb") as f:
                    model = load_model(f.read())
            except OSError as e:
                raise QgsProcessingException(tr("元のモデルを読めず: {e}").format(e=e))
            except TrainError as e:
                raise QgsProcessingException(tr(str(e)))
            if bands and (model.input.mode != "bands" or model.input.bands != bands):
                raise QgsProcessingException(tr("バンドの指定が元のモデル（{was}）と異なる。空欄で元のモデルのまま").format(
                    was=", ".join(map(str, model.input.bands)) if model.input.mode == "bands" else "表示のまま RGB"))
            if options.tile != model.tile:
                feedback.pushInfo(tr("入力の大きさは元のモデルの {t} 画素のまま").format(t=model.tile))
            options.tile = model.tile
            options.size = model.widths
            classes = class_order(found, self._taxonomy, model.classes)
            new = add_classes(model, classes)
            options.lr = rate or 1e-3
            feedback.pushInfo(tr("追加学習: {base}（{n} クラス{new}）").format(
                base=os.path.basename(self._base_path), n=len(model.classes),
                new=tr("、新しいクラス {c}").format(c="・".join(new)) if new else ""))
        else:
            classes = class_order(found, self._taxonomy)
            spec = InputSpec("bands", bands) if bands else InputSpec()
            try:
                model = new_model(classes, spec, options.tile, options.size, self.parameterAsInt(parameters, self.DEPTH, context))
            except TrainError as e:
                raise QgsProcessingException(tr(str(e)))
            options.lr = rate or options.lr
            new = []
        model.depth = self.parameterAsInt(parameters, self.DEPTH, context)
        feedback.pushInfo(tr("クラス: {c}").format(c="・".join(model.classes)))
        feedback.pushInfo(tr("入力: {input}・{t}×{t} 画素").format(
            input=tr("バンド {b} の値").format(b=", ".join(map(str, model.input.bands))) if model.input.mode == "bands" else tr("表示のまま（RGB）"),
            t=model.tile))

        # The windows of every image.
        rng = np.random.default_rng(options.seed)
        windows = []
        counts: dict[str, int] = {}
        used_boxes = []
        names = []
        for i, image in enumerate(self._images):
            if feedback.isCanceled():
                return {}
            feedback.setProgressText(tr("画像を読み込み中… {name}").format(name=image["name"]))
            found_here, error = self._read(self._layers[i], image, model, gathered, rng, feedback)
            if error:
                raise QgsProcessingException(error)
            feedback.setProgress(10 * (i + 1) / len(self._images))
            if found_here is None:
                continue
            part, grid, kept = found_here
            for c in kept[:, 4].astype(int):
                counts[model.classes[c]] = counts.get(model.classes[c], 0) + 1
            windows += part.windows
            names.append(image["name"])
            used_boxes.append((image, grid, kept))
        if not windows:
            raise QgsProcessingException(tr("学習できる範囲なし。観測記録なら、記録した画像（同じ画像 ID）を指定"))
        if model.input.mode == "bands":
            if not model.input.mean:
                model.input.mean, model.input.std = band_statistics(windows)
                feedback.pushInfo(tr("バンドの正規化: 平均 {m}・標準偏差 {s}").format(
                    m=", ".join(f"{v:.4g}" for v in model.input.mean), s=", ".join(f"{v:.4g}" for v in model.input.std)))
            windows = [normalize(w, model.input) for w in windows]
        windows, dropped = thin(windows, rng)
        if dropped:
            feedback.pushWarning(tr("メモリに収まるよう範囲を {n} 件間引き").format(n=dropped))
        train_windows, check_windows = split(windows, options.validation, rng)
        feedback.pushInfo(tr("学習 {t} 範囲・検証 {c} 範囲・{e} エポック・バッチ {b}・学習率 {lr:g}").format(
            t=len(train_windows), c=len(check_windows), e=options.epochs, b=options.batch, lr=options.lr))

        try:
            run = train(model, train_windows, check_windows, options,
                        progress=lambda text, done: (feedback.setProgressText(tr(text)), feedback.setProgress(10 + 88 * done)),
                        log=lambda text: feedback.pushInfo(tr(text)), cancelled=feedback.isCanceled)
        except TrainError as e:
            raise QgsProcessingException(tr(str(e)))
        if run.stopped and run.epochs == 0:
            return {}
        if run.stopped:
            feedback.pushWarning(tr("中止。エポック {n} までの学習結果を保存").format(n=run.epochs))
        model.history.append(history_entry(run, names, counts, options, os.path.basename(self._base_path) if self._base_path else "", new))
        model.history[-1]["source"] = self._source_label

        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not path.lower().endswith(".onnx"):
            path += ".onnx"
        try:
            with open(path, "wb") as f:
                f.write(model.onnx())
        except OSError as e:
            raise QgsProcessingException(tr("モデルを保存できず: {e}").format(e=e))
        self._remember = self.parameterAsBool(parameters, self.REMEMBER, context)
        self._path = path
        if run.best is not None:
            feedback.pushInfo(tr("検証（エポック {e}）: 適合率 {p:.2f}・再現率 {r:.2f}・F1 {f:.2f}（{n} 件中）").format(
                e=run.best_epoch, p=run.best.precision, r=run.best.recall, f=run.best.f1, n=run.best.objects))
        feedback.pushInfo(tr("保存: {path}（{s:.0f} 秒）").format(path=path, s=time.monotonic() - started))

        answer = {self.OUTPUT: path}
        sink_crs = self._images[0]["crs"]
        fields = QgsFields()
        for name, kind in (("class", QVariant.String), ("image", QVariant.String)):
            fields.append(QgsField(name, kind))
        sink, dest = self.parameterAsSink(parameters, self.BOXES, context, fields, QgsWkbTypes.Type.Polygon, sink_crs)
        if sink is not None:
            for image, grid, boxes in used_boxes:
                to = QgsCoordinateTransform(image["crs"], sink_crs, self._transform_context)
                for cx, cy, w, h, c in boxes:
                    corners = [grid.to_map(cx + dx * w / 2, cy + dy * h / 2) for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1))]
                    g = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in corners]])
                    g.transform(to)
                    f = QgsFeature(fields)
                    f.setGeometry(g)
                    f.setAttributes([model.classes[int(c)], image["name"]])
                    sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            answer[self.BOXES] = dest
        feedback.setProgress(100)
        return answer

    def _read(self, layer, image: dict, model, gathered: Gathered, rng, feedback) -> tuple:
        """
        The windows of one image: ((windows, grid, boxes kept) or None when it
        has no objects, '' ) or (None, why it cannot be read). Errors are given,
        not raised, so no frame holding the layer outlives the run.
        """
        try:
            grid = grid_of(image["extent"], image["pixel"])
            if model.input.mode == "bands":
                check_bands(layer, model.input.bands)
        except AiError as e:
            return None, tr("{name}: {e}").format(name=image["name"], e=e)
        if model.input.mode == "bands":
            read = lambda extent, w, h: band_values(layer, model.input.bands, extent, w, h)  # noqa: E731
        else:
            read = lambda extent, w, h: render_rgba(layer, extent, w, h, image["crs"], self._transform_context)  # noqa: E731
        boxes = pixel_boxes(gathered.labels, grid, image["crs"], self._transform_context, model.classes, image["id"])
        if not len(boxes):
            feedback.pushWarning(tr("{name}: 物体なし（この画像の記録・地物がない）").format(name=image["name"]))
            return None, ""
        areas = area_geometries(gathered.areas, grid, image["crs"], self._transform_context, image["id"])
        part = windows_of(read, grid, boxes, areas, model.tile, rng, image["name"], feedback.isCanceled)
        if part.too_big:
            feedback.pushWarning(tr("{name}: 入力より大きな物体 {n} 件を除外（画素を大きく、または入力を大きく）").format(name=image["name"], n=part.too_big))
        kept = boxes[(boxes[:, 2] <= model.tile * 0.9) & (boxes[:, 3] <= model.tile * 0.9)]
        feedback.pushInfo(tr("{name}: 物体 {objects} 件・範囲 {windows}（背景のみ {neg}）・画素 {px:g}").format(
            name=image["name"], objects=len(kept), windows=len(part.windows), neg=part.negatives, px=image["pixel"]))
        return (part, grid, kept), ""

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        if getattr(self, "_remember", False) and getattr(self, "_path", ""):
            from ..qgis_io.settings import remember_trained_model

            remember_trained_model(os.path.splitext(os.path.basename(self._path))[0], self._path)
        return {}


def default_inputs(layers: list, active=None) -> dict:
    """The dialog's first choice: the active image (else the visible images)."""
    images = [l for l in layers if isinstance(l, QgsRasterLayer)]
    if not images:
        return {}
    return {TrainAlgorithm.IMAGES: [active.id()] if active in images else [images[0].id()]}

