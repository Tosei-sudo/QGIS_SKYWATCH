"""
QGIS の式の SKYWATCH 関数 (tsudo, 2026-11-08): the plugin's data where power
users already work, in the field calculator, labels, styles, 「式で選択」 and
virtual fields (group 「SKYWATCH」 of the expression builder).

- skywatch_record_count(画像ID[, 種別]): the records of an image in the
  observation dock's store (present, not deleted; 種別: that class or below).
- skywatch_object_names(画像ID[, 種別]): the names of the things recorded there.
- skywatch_last_seen(モノ): when a thing (its id or name) was last seen there.
- skywatch_last_activity(モノ): its activity then.
- skywatch_kind_level(種別, 階層): one level of a class path (1 = 大分類, -1 the last).
- skywatch_place(図形) / skywatch_places(図形): the named area(s) the
  shape's point on surface is in, smallest first (alertReports.namedAreas,
  else observationExports.namedAreas).

The functions only read what the plugin holds (expressions are evaluated in
the map's render threads too, where no request can wait): the records the
observation dock read, and the named areas read in the background when the
plugin starts, the settings file changes and every 10 minutes (NULL until
they are read).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression, QgsGeometry, QgsProject, qgsfunction
from qgis.PyQt.QtCore import QDateTime, QObject, Qt, QTimer

from ..core.obs.alerts import PLACE_CACHE_SECONDS, load_named_areas, places_of
from ..core.obs.flows import living
from ..core.obs.taxonomy import path_of, within

GROUP = "SKYWATCH"

#: The observation dock's book (its records), set by the plugin.
_book: Callable[[], Any] = lambda: None
#: The named areas read last (WGS 84 polygons).
_areas: list = []


def set_book(book: Callable[[], Any]) -> None:
    global _book
    _book = book


def set_named_areas(areas: list) -> None:
    global _areas
    _areas = list(areas or [])


def _records() -> list:
    book = _book()
    if book is None:
        return []
    return living(list(getattr(book, "records", []) or []))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _here(image_id: Any, kind: Any = "") -> list:
    image = _text(image_id)
    path = path_of(_text(kind))
    return [o for o in _records() if o.present and o.image_id == image and (not path or within(o.kind, path))]


def _flow(thing: Any) -> list:
    key = _text(thing)
    if not key:
        return []
    rows = [o for o in _records() if o.object_id == key] or [o for o in _records() if o.object_name == key]
    return sorted((o for o in rows if o.present and o.image_time is not None), key=lambda o: (o.image_time, o.updated_at))


def record_count(image_id: Any, kind: Any = "") -> int:
    return len(_here(image_id, kind))


def object_names(image_id: Any, kind: Any = "") -> list[str]:
    return list(dict.fromkeys(o.object_name for o in sorted(_here(image_id, kind), key=lambda o: o.object_name)))


def last_seen(thing: Any) -> Optional[QDateTime]:
    rows = _flow(thing)
    return QDateTime.fromMSecsSinceEpoch(int(rows[-1].image_time), Qt.TimeSpec.UTC) if rows else None


def last_activity(thing: Any) -> Optional[str]:
    rows = _flow(thing)
    return rows[-1].activity if rows else None


def kind_level(kind: Any, level: Any = 1) -> Optional[str]:
    path = path_of(_text(kind))
    try:
        n = int(level)
    except (TypeError, ValueError):
        return None
    i = n - 1 if n > 0 else len(path) + n
    return path[i] if 0 <= i < len(path) and n != 0 else None


def _point(geometry: Any, context: Any) -> Optional[tuple[float, float]]:
    """The shape's point on its surface in WGS 84 (the layer's CRS from the expression's context, else the project's)."""
    if not isinstance(geometry, QgsGeometry) or geometry.isNull() or geometry.isEmpty():
        return None
    authid = ""
    if context is not None:
        for name in ("layer_crs", "project_crs"):
            value = context.variable(name)
            if value:
                authid = str(value)
                break
    crs = QgsCoordinateReferenceSystem(authid) if authid else QgsProject.instance().crs()
    point = geometry.pointOnSurface()
    if point.isNull():
        return None
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs.isValid() and crs != wgs:
        try:
            point.transform(QgsCoordinateTransform(crs, wgs, QgsProject.instance()))
        except Exception:  # noqa: BLE001 (outside the CRS's area: no place)
            return None
    p = point.asPoint()
    return p.x(), p.y()


def places(geometry: Any, context: Any = None) -> list[str]:
    at = _point(geometry, context)
    if at is None or not _areas:
        return []
    return list(dict.fromkeys(a.name for a in places_of(at[0], at[1], _areas)))


# ---- the functions as QGIS has them ---------------------------------------------------------------

_DOC = {
    "skywatch_record_count": ("skywatch_record_count(画像ID[, 種別])",
                              "観測記録ドックで選んだ保存先の、その画像の記録の件数（在のみ・削除を除く）。種別を渡すとその分類と下位だけ。"
                              "<br>例: <code>skywatch_record_count(\"IMG_ID\", '船舶')</code>"),
    "skywatch_object_names": ("skywatch_object_names(画像ID[, 種別])", "その画像に記録したモノの名前の配列。<br>例: "
                              "<code>array_to_string(skywatch_object_names(\"IMG_ID\"), '、')</code>"),
    "skywatch_last_seen": ("skywatch_last_seen(モノ)", "モノ（ID か名前）を最後に確認した画像の撮像日時。<br>例: "
                           "<code>skywatch_last_seen(\"OBJ_ID\")</code>"),
    "skywatch_last_activity": ("skywatch_last_activity(モノ)", "モノを最後に確認したときの活動。"),
    "skywatch_kind_level": ("skywatch_kind_level(種別, 階層)", "種別（船舶 > 支援船 > 〇〇級）の 1 つの階層。1 が大分類、-1 が最も下。"
                            "<br>例: <code>skywatch_kind_level(\"KIND\", 1)</code>"),
    "skywatch_place": ("skywatch_place(図形)", "図形（の内側の点）を含む地名のポリゴンのうち最も小さいものの名前（設定ファイルの namedAreas）。"
                       "地名はプラグインの起動時と 10 分ごとに読む（読む前は NULL）。<br>例: <code>skywatch_place($geometry)</code>"),
    "skywatch_places": ("skywatch_places(図形)", "図形を含む地名の配列（小さい順）。"),
}


def _help(name: str) -> str:
    title, text = _DOC[name]
    return f"<h3>{title}</h3>{text}"


@qgsfunction(group=GROUP, register=False, handlesnull=True, helpText=_help("skywatch_record_count"))
def skywatch_record_count(image_id, kind="", feature=None, parent=None):  # noqa: ARG001 (QGIS's arguments)
    return record_count(image_id, kind)


@qgsfunction(group=GROUP, register=False, handlesnull=True, helpText=_help("skywatch_object_names"))
def skywatch_object_names(image_id, kind="", feature=None, parent=None):  # noqa: ARG001
    return object_names(image_id, kind)


@qgsfunction(group=GROUP, register=False, helpText=_help("skywatch_last_seen"))
def skywatch_last_seen(thing, feature=None, parent=None):  # noqa: ARG001
    return last_seen(thing)


@qgsfunction(group=GROUP, register=False, helpText=_help("skywatch_last_activity"))
def skywatch_last_activity(thing, feature=None, parent=None):  # noqa: ARG001
    return last_activity(thing)


@qgsfunction(group=GROUP, register=False, helpText=_help("skywatch_kind_level"))
def skywatch_kind_level(kind, level=1, feature=None, parent=None):  # noqa: ARG001
    return kind_level(kind, level)


@qgsfunction(group=GROUP, register=False, usesgeometry=True, helpText=_help("skywatch_place"))
def skywatch_place(geometry, feature=None, parent=None, context=None):  # noqa: ARG001
    found = places(geometry, context)
    return found[0] if found else None


@qgsfunction(group=GROUP, register=False, usesgeometry=True, helpText=_help("skywatch_places"))
def skywatch_places(geometry, feature=None, parent=None, context=None):  # noqa: ARG001
    return places(geometry, context)


FUNCTIONS = [skywatch_record_count, skywatch_object_names, skywatch_last_seen, skywatch_last_activity, skywatch_kind_level, skywatch_place,
             skywatch_places]
NAMES = [f.name() for f in FUNCTIONS]


def register() -> None:
    for f in FUNCTIONS:
        if not QgsExpression.isFunctionName(f.name()):
            QgsExpression.registerFunction(f)


def unregister() -> None:
    for f in FUNCTIONS:
        if QgsExpression.isFunctionName(f.name()):
            QgsExpression.unregisterFunction(f.name())
    set_book(lambda: None)
    set_named_areas([])


def named_source(config: Any):
    """Where the named areas are (alertReports.namedAreas, else observationExports.namedAreas)."""
    for owner in (getattr(config, "alert_reports", None), getattr(config, "observation_exports", None)):
        source = getattr(owner, "named_areas", None)
        if source is not None:
            return source
    return None


class PlaceReader(QObject):
    """Reads the named areas in the background (now, when the settings file changes, every 10 minutes)."""

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.task = None
        self.read_at = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(int(PLACE_CACHE_SECONDS * 1000))
        self.timer.timeout.connect(self.read)
        context.configChanged.connect(self.read)

    def start(self) -> None:
        self.timer.start()
        self.read()

    def stop(self) -> None:
        self.timer.stop()
        try:
            self.context.configChanged.disconnect(self.read)
        except (TypeError, RuntimeError):
            pass
        if self.task is not None:
            try:
                self.task.cancel()
            except RuntimeError:
                pass
        self.task = None

    def read(self) -> None:
        from .tasks import run_task

        source = named_source(self.context.config)
        if source is None:
            set_named_areas([])
            return
        if self.task is not None:
            return
        client = self.context.esri()
        token = self.context.token_for(source.url, source.token)

        def done(areas) -> None:
            self.task = None
            self.read_at = time.monotonic()
            set_named_areas(areas)

        def failed(_message: str) -> None:
            # Kept as they were (a place name later is better than none now).
            self.task = None

        self.task = run_task("地名の読み込み（式の関数）", lambda _t: load_named_areas(client, source, token), done, failed, hidden=True)
