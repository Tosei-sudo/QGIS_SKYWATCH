"""
アペンド（既存レイヤーへ追加） as a Processing algorithm (`mikage:append`), as
ArcGIS's Append tool in a model: the features of the input layers added to
a target that is there already (a layer of the project, or an ArcGIS feature
layer by its URL), with a field map (a name kept in 「アペンド」's window, or
its JSON; empty: by the names), a schema type, key fields whose matching
features are updated, and 「全部か無しか」.

Threads: the layers are taken on the main thread (prepareAlgorithm); the
features read, the plan made and an Esri layer written in the background
(processAlgorithm: the Processing toolbox's dialog runs it there; Esri is not
asked on the main thread); a QGIS layer written on the main thread when it
ends (postProcessAlgorithm).
"""
from __future__ import annotations

import os
from typing import Any, Callable

from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber, QgsProcessingOutputString,
    QgsProcessingParameterBoolean, QgsProcessingParameterDefinition, QgsProcessingParameterEnum, QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString, QgsProcessingParameterVectorLayer, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core import append as ap

HERE = os.path.dirname(os.path.dirname(__file__))
SCHEMAS = [ap.NO_TEST, ap.TEST, ap.TEST_AND_SKIP]
#: Skipped features told one by one in the log, at most.
TOLD = 30

#: The plugin's context (its settings file, Esri client and tokens), set by the plugin.
_context: Callable[[], Any] = lambda: None


def tr(text: str) -> str:
    return QCoreApplication.translate("Mikage", text)


def set_context(context: Callable[[], Any]) -> None:
    global _context
    _context = context


def kept_or_json(text: str, problems: list) -> ap.FieldMap:
    """A field map from a name kept in 「アペンド」's window, or from its JSON (empty: none)."""
    text = (text or "").strip()
    if not text:
        return ap.FieldMap()
    if text.startswith("{"):
        return ap.map_from_json(text, problems)
    from ..qgis_io.prefs.maps import append_maps

    kept = append_maps().get(text)
    if kept is None:
        raise QgsProcessingException(tr("保存したフィールドマップ「{n}」がない").format(n=text))
    return ap.map_from_json(kept, problems)


class AppendAlgorithm(QgsProcessingAlgorithm):
    INPUTS = "INPUTS"
    TARGET = "TARGET"
    TARGET_URL = "TARGET_URL"
    SCHEMA_TYPE = "SCHEMA_TYPE"
    FIELD_MAP = "FIELD_MAP"
    SELECTED = "SELECTED"
    CONDITION = "CONDITION"
    MATCH_FIELDS = "MATCH_FIELDS"
    UPDATE_GEOMETRY = "UPDATE_GEOMETRY"
    ENFORCE_DOMAINS = "ENFORCE_DOMAINS"
    TRUNCATE = "TRUNCATE"
    ALL_OR_NOTHING = "ALL_OR_NOTHING"
    OUTPUT = "OUTPUT"
    APPENDED = "APPENDED"
    UPDATED = "UPDATED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"

    def __init__(self):
        super().__init__()
        self._job = None
        self._client = None
        self._token = None
        self._plan = None
        self._applied = None
        self._kept = ap.FieldMap()
        self._given_map = False

    def createInstance(self) -> "AppendAlgorithm":  # noqa: N802 (QGIS's name)
        return AppendAlgorithm()

    def name(self) -> str:
        return "append"

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return tr("アペンド（既存レイヤーへ追加）")

    def group(self) -> str:
        return tr("レイヤー")

    def groupId(self) -> str:  # noqa: N802 (QGIS's name)
        return "layers"

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "append.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        return tr(
            "入力レイヤーの地物を、既にある追加先レイヤーに追加（ArcGIS の「追加（Append）」と同じ）。\n\n"
            "追加先: プロジェクトのレイヤー（GeoPackage・Shapefile・PostGIS・スクラッチなど）か、ArcGIS フィーチャレイヤーの URL。"
            "ArcGIS へはミカゲの書き込みと同じく、作成者・更新者の列・監査ログ・タイムゾーンの補正つきで送信。座標系は追加先に変換。\n"
            "フィールドマップ: 「アペンド（既存レイヤーへ追加）…」の画面で保存した名前か、その JSON。空なら列名（大文字小文字・エイリアスも）で対応。"
            "JSON にない追加先の列も列名で対応。\n"
            "キー列: 追加先の列名（コンマ区切り）。同じ値の地物が追加先にあれば更新、なければ追加。\n"
            "全部か無しか: 1 件でも失敗したら、今回の分をすべて取り消し。\n"
            "ミカゲ自身の表（観測記録・撮像依頼など）は、管理者の担当だけが追加先にできる。"
        )

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        self.addParameter(QgsProcessingParameterMultipleLayers(self.INPUTS, tr("入力レイヤー"), QgsProcessing.SourceType.TypeVector))
        self.addParameter(QgsProcessingParameterVectorLayer(self.TARGET, tr("追加先のレイヤー"), [QgsProcessing.SourceType.TypeVector],
                                                            optional=True))
        self.addParameter(QgsProcessingParameterString(self.TARGET_URL, tr("追加先の ArcGIS フィーチャレイヤーの URL（レイヤーの代わりに）"),
                                                       optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.SCHEMA_TYPE, tr("列の合わせ方（スキーマタイプ）"),
                                                     [tr(ap.SCHEMA_NAMES[s]) for s in SCHEMAS], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.FIELD_MAP, tr("フィールドマップ（保存した名前か JSON。空なら列名で対応）"),
                                                       multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.SELECTED, tr("選択した地物のみ"), False))
        self.addParameter(QgsProcessingParameterString(self.CONDITION, tr("絞り込みの式（各入力に）"), optional=True))
        self.addParameter(QgsProcessingParameterString(self.MATCH_FIELDS, tr("一致する地物を更新するキー列（追加先の列名、コンマ区切り）"),
                                                       optional=True))
        advanced = [
            QgsProcessingParameterBoolean(self.UPDATE_GEOMETRY, tr("更新する地物の形状も更新"), False),
            QgsProcessingParameterBoolean(self.ENFORCE_DOMAINS, tr("ドメインに合わない値の地物は飛ばす"), True),
            QgsProcessingParameterBoolean(self.TRUNCATE, tr("長い文字列は切り詰めて追加"), False),
        ]
        for p in advanced:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
            self.addParameter(p)
        self.addParameter(QgsProcessingParameterBoolean(self.ALL_OR_NOTHING, tr("1 件でも失敗したら今回の分をすべて取り消し"), True))
        self.addOutput(QgsProcessingOutputString(self.OUTPUT, tr("追加先")))
        for name, label in ((self.APPENDED, "追加した地物数"), (self.UPDATED, "更新した地物数"), (self.SKIPPED, "飛ばした地物数"),
                            (self.FAILED, "失敗した地物数")):
            self.addOutput(QgsProcessingOutputNumber(name, tr(label)))

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        from ..qgis_io import append_io

        layers = [x for x in self.parameterAsLayerList(parameters, self.INPUTS, context) if isinstance(x, QgsVectorLayer)]
        if not layers:
            raise QgsProcessingException(tr("入力レイヤーを選択"))
        url = (self.parameterAsString(parameters, self.TARGET_URL, context) or "").strip()
        target_layer = self.parameterAsVectorLayer(parameters, self.TARGET, context)
        if url:
            target = append_io.target_for_url(url)
        elif target_layer is not None:
            if any(x.id() == target_layer.id() for x in layers):
                raise QgsProcessingException(tr("追加先と同じレイヤーは入力にできない"))
            target = append_io.target_for_layer(target_layer)
        else:
            raise QgsProcessingException(tr("追加先のレイヤーか URL を指定"))
        plugin = _context()
        self._client, self._token = None, None
        if target.is_esri:
            if not url.lower().startswith(("http://", "https://")) and not target.url.lower().startswith(("http://", "https://")):
                raise QgsProcessingException(tr("追加先の URL は http:// か https:// で始まる"))
            if plugin is None:
                raise QgsProcessingException(tr("ArcGIS へ追加するにはプラグインの設定ファイルが必要"))
            role = append_io.own_table_of(target.url, plugin)
            provider = self.provider()
            allowed = getattr(provider, "allowed", None)
            if role and callable(allowed) and not allowed("append.own_tables"):
                from ..core.errors import tagged, text

                raise QgsProcessingException(tagged(text("E206", f"（「{role}」はミカゲの画面で書く表。追加先にできるのは管理者の担当だけ）"), "E206"))
            self._client = plugin.esri()
            self._token = plugin.token_for(target.url, plugin.token_of(target.url))
        selected = self.parameterAsBoolean(parameters, self.SELECTED, context)
        condition = self.parameterAsString(parameters, self.CONDITION, context) or ""
        inputs = []
        names: set = set()
        for layer in layers:
            name = layer.name()
            n = 2
            while name in names:
                name, n = f"{layer.name()} ({n})", n + 1
            names.add(name)
            source = append_io.InputSource(layer, selected, condition, name)
            problem = source.condition_problem()
            if problem:
                raise QgsProcessingException(problem)
            inputs.append(source)
        problems: list = []
        self._kept = kept_or_json(self.parameterAsString(parameters, self.FIELD_MAP, context), problems)
        for p in problems:
            feedback.pushWarning(tr("フィールドマップ: {p}").format(p=p))
        options = ap.Options(
            schema_type=SCHEMAS[self.parameterAsEnum(parameters, self.SCHEMA_TYPE, context)],
            match_fields=[m.strip() for m in (self.parameterAsString(parameters, self.MATCH_FIELDS, context) or "").split(",") if m.strip()],
            update_geometry=self.parameterAsBoolean(parameters, self.UPDATE_GEOMETRY, context),
            enforce_domains=self.parameterAsBoolean(parameters, self.ENFORCE_DOMAINS, context),
            truncate=self.parameterAsBoolean(parameters, self.TRUNCATE, context),
            all_or_nothing=self.parameterAsBoolean(parameters, self.ALL_OR_NOTHING, context))
        self._job = append_io.Job(inputs, target, ap.FieldMap(), options)
        self._plan, self._applied = None, None
        return True

    def processAlgorithm(self, parameters, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        from ..qgis_io import append_io
        from ..qgis_io.tasks import Cancelled

        job = self._job
        target = job.target

        def check() -> None:
            if feedback.isCanceled():
                raise Cancelled()

        if target.is_esri:
            try:
                append_io.describe_esri(target, self._client, self._token)
            except RuntimeError:
                # (The plugin asks Esri only off the main thread: qgis_process and processing.run run here.)
                raise QgsProcessingException(tr("ArcGIS への追加はバックグラウンドで実行（処理ツールボックスのダイアログから）")) from None
            except Exception as error:  # noqa: BLE001 (a layer that cannot be read is told)
                raise QgsProcessingException(tr("追加先の取得に失敗: {m}").format(m=error)) from None
        schema = target.schema
        feedback.pushInfo(tr("追加先: {n}（{g}・列 {c} 個）").format(n=target.name, g=ap.GEOMETRY_NAMES.get(schema.geometry, ""),
                                                                 c=len(schema.mappable)))
        missing = [m for m in job.options.match_fields if schema.field(m) is None]
        if missing:
            raise QgsProcessingException(tr("追加先にキー列がない: {m}").format(m="、".join(missing)))
        inputs = [(s.name, s.fields) for s in job.inputs]
        problems: list = []
        job.field_map, problems = ap.fit_map(self._kept, schema, inputs) if self._kept.entries else (ap.auto_map(schema, inputs), [])
        for p in problems:
            feedback.pushWarning(tr("フィールドマップ: {p}").format(p=p))
        try:
            the_plan = append_io.prepare(job, self._client, self._token, check, lambda v: feedback.setProgress(v * 0.5))
        except Cancelled:
            raise QgsProcessingException(tr("取消")) from None
        for name, why in the_plan.skipped_inputs.items():
            feedback.pushWarning(tr("列が一致しない入力「{n}」: {w}").format(n=name, w=why))
        if the_plan.stopped:
            raise QgsProcessingException(tr("列が一致しない入力があるため中止（何も追加していない）"))
        feedback.pushInfo(the_plan.summary())
        skipped = [p for p in the_plan.rows if p.action == ap.SKIP]
        for p in skipped[:TOLD]:
            feedback.pushWarning(tr("飛ばす: {i} の {n}: {r}").format(i=p.input, n=p.number, r="、".join(p.reasons)))
        if len(skipped) > TOLD:
            feedback.pushWarning(tr("ほか {n} 件を飛ばす").format(n=f"{len(skipped) - TOLD:,}"))
        self._plan = the_plan
        if target.is_esri and the_plan.writes:
            try:
                self._applied = append_io.write_esri(job, the_plan, self._client, self._token, check,
                                                     lambda v: feedback.setProgress(50 + v * 0.5))
            except Exception as error:  # noqa: BLE001
                raise QgsProcessingException(tr("追加に失敗: {m}").format(m=error)) from None
            self._tell(feedback)
        return self._outputs()

    def postProcessAlgorithm(self, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        from ..qgis_io import append_io

        job, the_plan = self._job, self._plan
        if job is None or the_plan is None:
            return self._outputs()
        if job.target.is_esri:
            append_io.reload_shown(job.target)
            return self._outputs()
        if the_plan.writes:
            layer = append_io.target_layer(job.target, context.project()) or append_io.target_layer(job.target)
            try:
                self._applied = append_io.write_layer(job, the_plan, layer)
            except ValueError as error:
                raise QgsProcessingException(str(error)) from None
            self._tell(feedback)
        return self._outputs()

    def _tell(self, feedback) -> None:
        """What the write did, in the log; a write taken back whole fails the algorithm (a model stops there)."""
        a = self._applied
        if a is None:
            return
        for p, why in a.failed[:TOLD]:
            feedback.pushWarning(tr("失敗: {i} の {n}: {w}").format(i=p.input, n=p.number, w=why))
        if a.rolled_back:
            raise QgsProcessingException(a.text())
        (feedback.reportError if a.failed else feedback.pushInfo)(a.text())

    def _outputs(self) -> dict:
        job, the_plan, a = self._job, self._plan, self._applied
        added = len(a.added) if a is not None and not a.rolled_back else 0
        updated = len(a.updated) if a is not None and not a.rolled_back else 0
        failed = len(a.failed) if a is not None else 0
        skipped = the_plan.count(ap.SKIP) if the_plan is not None else 0
        out = job.target.url if job is not None and job.target.is_esri else (job.target.layer_id if job is not None else "")
        return {self.OUTPUT: out, self.APPENDED: added, self.UPDATED: updated, self.SKIPPED: skipped, self.FAILED: failed}
