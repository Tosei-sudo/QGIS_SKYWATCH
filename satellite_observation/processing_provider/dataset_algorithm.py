"""
「教師データセットの作成」 as a Processing algorithm: a training dataset
(core.ai.train.dataset) of images outside the catalog and their annotations,
written as a small JSON file of their paths (the files stay where they are),
for 「AI 学習（物体検出）」 to train on.

Run again on the same file to add more folders (a group each, images the
dataset has already left out).
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
    QgsProcessingParameterFile, QgsProcessingParameterFileDestination, QgsProcessingParameterString, QgsProcessingOutputNumber,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.ai.train.dataset import (
    Dataset, DatasetError, add_group, load_dataset, make_group, missing_files, parse_class_map, save_dataset,
)
from ..qgis_io.dataset_io import class_counts, class_field_of, vector_layer

HERE = os.path.dirname(os.path.dirname(__file__))
#: The formats offered (自動 first).
FORMATS = ("", "yolo", "dota", "voc", "coco", "vector")


def tr(text: str) -> str:
    return QCoreApplication.translate("SatelliteObservation", text)


class DatasetAlgorithm(QgsProcessingAlgorithm):
    IMAGES = "IMAGES"
    LABELS = "LABELS"
    LABELS_FILE = "LABELS_FILE"
    FORMAT = "FORMAT"
    NAMES = "NAMES"
    CLASS_FIELD = "CLASS_FIELD"
    CLASS_MAP = "CLASS_MAP"
    RECURSIVE = "RECURSIVE"
    BACKGROUND = "BACKGROUND"
    NAME = "NAME"
    OUTPUT = "OUTPUT"
    COUNT = "COUNT"

    def createInstance(self) -> "DatasetAlgorithm":  # noqa: N802 (QGIS's name)
        return DatasetAlgorithm()

    def name(self) -> str:
        return "make_dataset"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("教師データセットの作成")

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "ai_train.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "カタログにない画像とその注釈（アノテーション）を、「AI 学習（物体検出）」の教師データセットにまとめる。"
            "保存するのは画像と注釈のファイルの場所（絶対パス）と対応だけで、画像は複製しない。\n\n"
            "注釈の形式: YOLO（画像ごとの .txt、クラス名は classes.txt・data.yaml などから、または下の欄。OBB も可）、"
            "DOTA（.txt）、Pascal VOC（.xml）、COCO（全画像で 1 つの .json）、ベクタ（GeoJSON・GeoPackage など、地図座標。"
            "画像ごと、または全画像で 1 つ）。回転した枠は外接する枠で学習。\n"
            "対応: 画像と同じ名前の注釈（images/… と labels/… のような並びも可）。\n"
            "クラスの対応: 注釈のクラス名を分類体系の種別に（例 ship=船舶, plane=航空機 > 輸送機）。書かない名前はそのまま種別"
            "（分類の名前・別名でも可）。\n"
            "保存先に既存のデータセットを指定すると追記（同じ画像は追加しない）。\n"
            "ファイルを移動したら作り直す（見つからないファイルは学習時に除外）。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterFile(self.IMAGES, tr("画像のフォルダー"), QgsProcessingParameterFile.Behavior.Folder))
        self.addParameter(QgsProcessingParameterFile(self.LABELS, tr("注釈のフォルダー（空で画像と同じ）"),
                                                     QgsProcessingParameterFile.Behavior.Folder, optional=True))
        self.addParameter(QgsProcessingParameterFile(self.LABELS_FILE, tr("全画像の注釈ファイル（COCO の .json・ベクタ、フォルダーの代わり）"),
                                                     QgsProcessingParameterFile.Behavior.File, optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.FORMAT, tr("注釈の形式"),
                                                     [tr("自動"), "YOLO", "DOTA", "Pascal VOC", "COCO", tr("ベクタ（地図座標）")], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.NAMES, tr("YOLO のクラス名（番号順、カンマ区切り。空で classes.txt など）"),
                                                       optional=True))
        self.addParameter(QgsProcessingParameterString(self.CLASS_FIELD, tr("ベクタのクラスのフィールド（空で class・種別 など）"), optional=True))
        self.addParameter(QgsProcessingParameterString(self.CLASS_MAP, tr("クラスの対応（例 ship=船舶, plane=航空機 > 輸送機）"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.RECURSIVE, tr("下のフォルダーも含める"), True))
        self.addParameter(QgsProcessingParameterBoolean(self.BACKGROUND, tr("注釈のない画像も背景として含める"), False))
        self.addParameter(QgsProcessingParameterString(self.NAME, tr("データセットの名前（空でファイル名）"), optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("教師データセット（追記は既存のファイルを指定）"),
                                                                tr("教師データセット (*.json)")))
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, tr("追加した画像")))

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        images = self.parameterAsFile(parameters, self.IMAGES, context)
        labels = self.parameterAsFile(parameters, self.LABELS_FILE, context) or self.parameterAsFile(parameters, self.LABELS, context)
        fmt = FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        names = [n.strip() for n in (self.parameterAsString(parameters, self.NAMES, context) or "").split(",") if n.strip()]
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            class_map = parse_class_map(self.parameterAsString(parameters, self.CLASS_MAP, context))
            dataset = load_dataset(path) if os.path.isfile(path) else Dataset()
            found = make_group(images, labels, fmt, names, self.parameterAsString(parameters, self.CLASS_FIELD, context).strip(), class_map,
                               self.parameterAsBool(parameters, self.RECURSIVE, context), self.parameterAsBool(parameters, self.BACKGROUND, context))
            group = found.group
            if group.format == "vector":
                # The class field now, so a wrong one is told before training.
                group.class_field = class_field_of(vector_layer(group.items[0].labels), group.class_field) if group.items else group.class_field
        except DatasetError as e:
            raise QgsProcessingException(tr(str(e)))
        if dataset.groups:
            feedback.pushInfo(tr("既存のデータセットに追記: {n} 画像").format(n=len(dataset.items)))
        feedback.pushInfo(tr("注釈の形式: {f}・画像 {n}").format(f=group.format.upper(), n=len(group.items)))
        if found.alone:
            feedback.pushWarning(tr("注釈のない画像 {n} 枚を除外（例 {e}）").format(n=len(found.alone), e=os.path.basename(found.alone[0])))
        if found.unused:
            feedback.pushInfo(tr("画像のない注釈 {n} 件").format(n=found.unused))
        if group.format == "yolo":
            if group.names:
                feedback.pushInfo(tr("YOLO のクラス: {c}").format(c=", ".join(group.names)))
            else:
                feedback.pushWarning(tr("YOLO のクラス名が分からない（classes.txt・data.yaml なし）: クラスは番号のまま。"
                                        "クラス名の欄か、クラスの対応（0=船舶 など）で指定"))
        if not group.items:
            raise QgsProcessingException(tr("画像と注釈の組がない（同じ名前のファイルが必要）"))
        offered = len(group.items)
        left = add_group(dataset, group)
        if left:
            feedback.pushInfo(tr("データセットにある画像 {n} 枚は追加せず").format(n=left))
        added = offered - left
        dataset.name = self.parameterAsString(parameters, self.NAME, context).strip() or dataset.name or os.path.splitext(os.path.basename(path))[0]
        try:
            counts = class_counts(dataset)
        except DatasetError as e:
            raise QgsProcessingException(tr(str(e)))
        dataset.stats = {"images": len(dataset.items), "objects": sum(counts.values()), "classes": dict(sorted(counts.items()))}
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            to = dict((m for g in dataset.groups for m in g.class_map.items())).get(name)
            feedback.pushInfo(tr("  {name}{to}: {n}").format(name=name or tr("（クラスなし）"), to=f" → {to}" if to else "", n=n))
        missing = missing_files(dataset)
        if missing:
            feedback.pushWarning(tr("見つからないファイル {n} 件（例 {p}）").format(n=len(missing), p=missing[0]))
        try:
            save_dataset(dataset, path)
        except OSError as e:
            raise QgsProcessingException(tr("保存できず: {e}").format(e=e))
        feedback.pushInfo(tr("保存: {path}（画像 {n}・物体 {o}）").format(path=path, n=len(dataset.items), o=dataset.stats["objects"]))
        return {self.OUTPUT: path, self.COUNT: added}
