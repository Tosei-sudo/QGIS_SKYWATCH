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
observation dock read, and the named areas of the squares kept on this PC
(core/obs/place_cache.py, shared with 通報 and the export; 2026-11-10: the
whole layer is no longer read every 10 minutes). A point in a square not
kept yet is NULL at first: its square is looked up in the background, and
the map is drawn again once it is in (the field calculator: run it again).
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression, QgsGeometry, QgsProject, qgsfunction
from qgis.PyQt.QtCore import QDateTime, QObject, Qt, QTimer, pyqtSignal

from ..core.obs import place_cache
from ..core.obs.flows import living
from ..core.obs.taxonomy import path_of, within

GROUP = "SKYWATCH"

#: The observation dock's book (its records), set by the plugin.
_book: Callable[[], Any] = lambda: None
#: Where the named areas are (the settings file's), and who looks up a square not kept (PlaceReader; any thread).
_source: Any = None
_want: Optional[Callable[[float, float], None]] = None
#: The squares asked for and not looked up yet (asked once, however many features are drawn there).
_asked: set = set()
_asked_lock = threading.Lock()


def _forget_asked(cells) -> None:
    with _asked_lock:
        _asked.difference_update(cells)


def set_book(book: Callable[[], Any]) -> None:
    global _book
    _book = book


def set_named_source(source: Any, want: Optional[Callable[[float, float], None]] = None) -> None:
    global _source, _want
    _source, _want = source, want
    with _asked_lock:
        _asked.clear()


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
    source = _source
    if source is None:
        return []
    at = _point(geometry, context)
    if at is None:
        return []
    found, again = place_cache.PLACES.peek(source, at[0], at[1])
    want = _want
    if again and want is not None:
        cell = place_cache.cell_of(at[0], at[1])
        with _asked_lock:
            first = cell not in _asked
            _asked.add(cell)
        if first:
            want(at[0], at[1])
    return list(dict.fromkeys(a.name for a in found or []))


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
                       "地名は使った場所（1 度四方）ごとに読んで PC に控える。初めての場所は NULL で、裏で読み終わると地図を描き直す"
                       "（フィールド計算機はもう一度実行）。<br>例: <code>skywatch_place($geometry)</code>"),
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
    set_named_source(None)


def named_source(config: Any):
    """Where the named areas are (alertReports.namedAreas, else observationExports.namedAreas)."""
    for owner in (getattr(config, "alert_reports", None), getattr(config, "observation_exports", None)):
        source = getattr(owner, "named_areas", None)
        if source is not None:
            return source
    return None


class PlaceReader(QObject):
    """
    Looks up, in the background, the squares the expression functions asked
    for (not kept, or no longer known to be good), a few at once; draws the
    map again when one was missing (its features were NULL). A square whose
    lookup failed is not asked again for a minute.
    """

    #: A point whose square is wanted (emitted from any thread; handled in the main thread).
    wanted = pyqtSignal(float, float)
    RETRY_SECONDS = 60.0

    def __init__(self, context, refresh: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.context = context
        self.refresh = refresh
        self.task = None
        #: The squares to look up next (a point of each).
        self.pending: dict = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.read)
        self.wanted.connect(self._want)
        context.configChanged.connect(self._configured)

    def start(self) -> None:
        self._configured()

    def stop(self) -> None:
        self.timer.stop()
        try:
            self.context.configChanged.disconnect(self._configured)
        except (TypeError, RuntimeError):
            pass
        set_named_source(None)
        if self.task is not None:
            try:
                self.task.cancel()
            except RuntimeError:
                pass
        self.task = None

    def _configured(self) -> None:
        source = named_source(self.context.config)
        self.pending.clear()
        set_named_source(source, self.wanted.emit if source is not None else None)

    def _want(self, lon: float, lat: float) -> None:
        self.pending[place_cache.cell_of(lon, lat)] = (lon, lat)
        if self.task is None and not self.timer.isActive():
            self.timer.start()

    def read(self) -> None:
        from .tasks import run_task

        source = named_source(self.context.config)
        if source is None or not self.pending or self.task is not None:
            return
        wanted, self.pending = self.pending, {}
        client = self.context.esri()
        token = self.context.token_for(source.url, source.token)
        points = list(wanted.values())

        def work(_t):
            missing = any(place_cache.PLACES.peek(source, lon, lat)[0] is None for lon, lat in points)
            place_cache.areas_at(client, source, points, token)
            return missing

        def finished(missing) -> None:
            self.task = None
            _forget_asked(wanted)
            if missing and self.refresh is not None:
                self.refresh()
            if self.pending:
                self.timer.start()

        def failed(_message: str) -> None:
            # Those squares asked again after a minute (a place name later is better than none now).
            self.task = None
            QTimer.singleShot(int(self.RETRY_SECONDS * 1000), lambda: _forget_asked(wanted))
            if self.pending:
                self.timer.start()

        self.task = run_task("地名の読み込み（式の関数）", work, finished, failed, hidden=True)
