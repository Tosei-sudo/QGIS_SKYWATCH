"""
保存した分析 as Processing algorithms (tsudo, 2026-11-08): each analysis kept
in the QGIS project by 「データ分析」 is an algorithm of the group
「保存した分析」 (id `skywatch:analysis_…`), so it runs from the Processing
toolbox, in batch, in the graphical modeler and from scripts
(`processing.run("skywatch:analysis_…", {...})`).

- Its `:name` values are text parameters (the value saved last as default).
- `OUTPUT` is the result as a layer (with shapes when the SQL gives the column
  SHAPE), which can be written to a file as any Processing output; `ROWS` how
  many rows.
- The tables are read as in the dock: Esri read-only, project layers through
  feature sources made on the main thread (`prepareAlgorithm`), the rows cap
  per table the parameter `LIMIT`.

The algorithms follow the project: another project read, an analysis saved or
deleted, and the list is made again (`refresh()`).
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Callable, Optional

from qgis.core import (
    QgsApplication, QgsFeatureSink, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber,
    QgsProcessingParameterDefinition, QgsProcessingParameterFeatureSink, QgsProcessingParameterNumber, QgsProcessingParameterString, QgsProject,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon

from ..core.analysis import (
    DEFAULT_TABLE_LIMIT, PROJECT_KEY, PROJECT_SCOPE, Analysis, AnalysisError, analyses_of, build_sql, esri_tables, parameters,
)

HERE = os.path.dirname(os.path.dirname(__file__))
GROUP = "保存した分析"
PREFIX = "analysis_"
OUTPUT = "OUTPUT"
ROWS = "ROWS"
LIMIT = "LIMIT"

#: The plugin's context (its settings file, Esri client and tokens), set by the plugin.
_context: Callable[[], Any] = lambda: None


def tr(text: str) -> str:
    return QCoreApplication.translate("Skywatch", text)


def set_context(context: Callable[[], Any]) -> None:
    global _context
    _context = context


def algorithm_name(name: str) -> str:
    """The Processing name of an analysis (stable for its name)."""
    return PREFIX + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def saved(project: Optional[QgsProject] = None) -> list[Analysis]:
    """The analyses kept in the project."""
    project = project or QgsProject.instance()
    text, _ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
    return analyses_of(text)


def sql_of(a: Analysis) -> str:
    """The SQL the analysis runs (the builder's when it has no SQL written)."""
    if a.sql and a.sql.strip():
        return a.sql
    return build_sql(a.builder)


def parameter_name(name: str, taken: set) -> str:
    """A Processing name of `:name` (letters, digits and _; not one of the algorithm's own)."""
    base = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in name) or "VALUE"
    out, n = base, 2
    while out.upper() in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out.upper())
    return out


def registered() -> list["AnalysisAlgorithm"]:
    """An algorithm per analysis of the project."""
    out, seen = [], set()
    for a in saved():
        name = algorithm_name(a.name)
        if name not in seen:
            seen.add(name)
            out.append(AnalysisAlgorithm(a.name))
    return out


def refresh() -> None:
    """The analyses changed (saved, deleted, another project): the provider's list made again."""
    registry = QgsApplication.processingRegistry()
    provider = registry.providerById("skywatch") if registry is not None else None
    if provider is not None:
        provider.refreshAlgorithms()


class _Progress:
    """What analysis_io.run takes as its task: cancelled and progress through the Processing feedback."""

    def __init__(self, feedback):
        self.feedback = feedback

    def isCanceled(self) -> bool:  # noqa: N802 (as QgsTask)
        return bool(self.feedback is not None and self.feedback.isCanceled())

    def progress(self, percent: float) -> None:
        if self.feedback is not None:
            self.feedback.setProgress(percent)


class AnalysisAlgorithm(QgsProcessingAlgorithm):
    def __init__(self, analysis_name: str):
        super().__init__()
        self._analysis_name = analysis_name
        self._analysis: Optional[Analysis] = next((a for a in saved() if a.name == analysis_name), None)
        self._params: dict[str, str] = {}
        self._prepared = None

    def createInstance(self) -> "AnalysisAlgorithm":  # noqa: N802 (QGIS's name)
        return AnalysisAlgorithm(self._analysis_name)

    def name(self) -> str:
        return algorithm_name(self._analysis_name)

    def displayName(self) -> str:  # noqa: N802 (QGIS's name)
        return self._analysis_name

    def group(self) -> str:
        return tr(GROUP)

    def groupId(self) -> str:  # noqa: N802 (QGIS's name)
        return "analysis"

    def icon(self) -> QIcon:
        return QIcon(os.path.join(HERE, "resources", "analysis.svg"))

    def shortHelpString(self) -> str:  # noqa: N802 (QGIS's name)
        sql = ""
        if self._analysis is not None:
            try:
                sql = sql_of(self._analysis)
            except AnalysisError as error:
                sql = str(error)
        return tr("「データ分析」でプロジェクトに保存した分析「{n}」を実行し、結果をレイヤーとして出力する。"
                  "SQL の :名前 は入力欄になる（既定値は最後に入れた値）。Esri の表を読む分析はバックグラウンドで実行する。").format(
            n=self._analysis_name) + ("\n\n" + sql if sql else "")

    def initAlgorithm(self, config=None) -> None:  # noqa: N802 (QGIS's name)
        taken = {OUTPUT, ROWS, LIMIT}
        self._params = {}
        if self._analysis is not None:
            try:
                names = parameters(sql_of(self._analysis))
            except AnalysisError:
                names = []
            for n in names:
                p = parameter_name(n, taken)
                self._params[n] = p
                self.addParameter(QgsProcessingParameterString(p, f":{n}", self._analysis.values.get(n) or None, False, False))
        limit = QgsProcessingParameterNumber(LIMIT, tr("1 つの表から読む上限（行）"), QgsProcessingParameterNumber.Type.Integer,
                                             DEFAULT_TABLE_LIMIT, False, 1, 10_000_000)
        limit.setFlags(limit.flags() | QgsProcessingParameterDefinition.Flag.FlagAdvanced)
        self.addParameter(limit)
        self.addParameter(QgsProcessingParameterFeatureSink(OUTPUT, tr("分析の結果"), QgsProcessing.SourceType.TypeVector))
        self.addOutput(QgsProcessingOutputNumber(ROWS, tr("結果の行数")))

    def prepareAlgorithm(self, parameters_, context, feedback) -> bool:  # noqa: N802 (QGIS's name)
        # On the main thread: the project's layers are read through feature sources made here.
        from ..qgis_io import analysis_io

        a = next((x for x in saved(context.project() or QgsProject.instance()) if x.name == self._analysis_name), None)
        if a is None:
            raise QgsProcessingException(tr("分析「{n}」がプロジェクトにない（削除された？）").format(n=self._analysis_name))
        try:
            sql = sql_of(a)
        except AnalysisError as error:
            raise QgsProcessingException(str(error)) from None
        values = {}
        for n in parameters(sql):
            v = self.parameterAsString(parameters_, self._params.get(n, n), context)
            if not v:
                raise QgsProcessingException(tr("値が未入力: :{n}").format(n=n))
            values[n] = v
        plugin = _context()
        config = getattr(plugin, "config", None)
        tables = esri_tables(config) if config is not None else []
        taken = {t.name.lower() for t in tables}
        project = context.project() or QgsProject.instance()
        tables += analysis_io.project_tables(project, taken)
        limit = self.parameterAsInt(parameters_, LIMIT, context) or DEFAULT_TABLE_LIMIT
        try:
            prepared = analysis_io.prepare(tables, sql, dict(a.conditions), limit, project)
        except AnalysisError as error:
            raise QgsProcessingException(str(error)) from None
        esri = [t for t, _r in prepared if t.source == "esri"]
        if esri and plugin is None:
            raise QgsProcessingException(tr("Esri の表を読むにはプラグインの設定ファイルが必要"))
        tokens = {t.name: plugin.token_for(t.url, t.token) for t in esri} if esri else {}
        client = plugin.esri() if esri else None
        self._prepared = (a, sql, values, prepared, tokens, client, limit)
        return True

    def processAlgorithm(self, parameters_, context, feedback) -> dict:  # noqa: N802 (QGIS's name)
        from ..qgis_io import analysis_io

        a, sql, values, prepared, tokens, client, limit = self._prepared
        feedback.pushInfo(tr("読む表: {t}").format(t="、".join(t.name for t, _r in prepared)))
        try:
            result, counts = analysis_io.run(client, prepared, sql, values, dict(a.conditions), lambda t: tokens.get(t.name), limit,
                                             _Progress(feedback))
        except AnalysisError as error:
            if feedback.isCanceled():
                return {OUTPUT: None, ROWS: 0}
            raise QgsProcessingException(str(error)) from None
        except RuntimeError as error:
            # Esri is asked off the main thread only (qgis_io/network.py).
            raise QgsProcessingException(tr("Esri の表を読む分析はバックグラウンドで実行する（処理ツールボックスのダイアログ・"
                                            "モデラー・バッチ処理から）: {m}").format(m=error)) from None
        for n, c in counts.items():
            feedback.pushInfo(tr("{n}: {c:,} 行").format(n=n, c=c))
        if result.cut:
            feedback.pushWarning(tr("結果は先頭 {n:,} 行まで").format(n=len(result.rows)))
        layer = analysis_io.result_layer(result, self._analysis_name)
        sink, dest = self.parameterAsSink(parameters_, OUTPUT, context, layer.fields(), layer.wkbType(), layer.crs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters_, OUTPUT))
        for f in layer.getFeatures():
            if feedback.isCanceled():
                break
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("結果 {n:,} 行").format(n=len(result.rows)))
        return {OUTPUT: dest, ROWS: len(result.rows)}
