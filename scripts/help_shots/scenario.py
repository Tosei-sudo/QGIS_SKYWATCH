"""
The screenshots of the help's 「画面つきの手順書」, taken in a real QGIS
against scripts/mock_esri (started by make_shots.py, which runs this with
`qgis --code`). Environment: SHOTS (folder for the pictures), FLOWS (which
flows, comma-separated: obs, request, plan, check, layer; default all), WORK (a folder
for files written on the way).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QDate, QRect, Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QDockWidget,
    QMessageBox,
    QToolBar,
    QWidget,
)
from qgis.utils import iface, plugins
from shots_lib import (
    Runner,
    fit_left,
    fit_right,
    global_rect,
    later,
    map_rect,
    save_button,
    moved,
    settle,
    shoot,
    shoot_screen,
    table_rows,
)

OUT = os.environ["SHOTS"]
WORK = os.environ.get("WORK", OUT)
FLOWS = [f for f in os.environ.get("FLOWS", "obs,request,plan,check,layer").split(",") if f]
LOG = open(os.path.join(OUT, "scenario.log"), "w", encoding="utf-8")
#: The width of the pictures of the whole window, as shown in the help.
WIDE = 720
#: The 観測記録 dock wide enough for its rows.
OBS_WIDTH = 660
#: The part of the map shown beside the 観測記録 dock.
OBS_MAP = 300
#: The part of the map shown beside the docks of the imaging plan.
PLAN_MAP = 260
#: The main window for each flow (the plan's docks shorter: less empty list below their rows).
WINDOW = {"obs": (1600, 1000), "request": (1600, 640), "plan": (1600, 800), "check": (1600, 820), "layer": (1600, 640)}
#: The name of the background map added for the pictures.
BASEMAP_NAME = "背景（見本）"


def log(text: str) -> None:
    print(text, file=LOG, flush=True)


win = iface.mainWindow()
canvas = iface.mapCanvas()


def path(name: str) -> str:
    return os.path.join(OUT, name + ".png")


def plugin():
    return plugins["mikage"]


def dock(name: str) -> QDockWidget:
    return plugin().docks[name]


def layout(left=(), right=(), widths=(440, 440), bottom=()):
    """Only these docks shown: the left ones tabbed on the left, the right ones on the right."""
    for d in win.findChildren(QDockWidget):
        if d.isVisible():
            d.hide()
    for area, docks in ((Qt.DockWidgetArea.LeftDockWidgetArea, left), (Qt.DockWidgetArea.RightDockWidgetArea, right),
                        (Qt.DockWidgetArea.BottomDockWidgetArea, bottom)):
        first = None
        for d in docks:
            d.setFloating(False)
            win.addDockWidget(area, d)
            if first is not None:
                win.tabifyDockWidget(first, d)
            first = first or d
            d.show()
    shown = [d for d in (list(left[:1]) + list(right[:1])) if d]
    if shown:
        win.resizeDocks(list(shown), list(widths[:len(shown)]), Qt.Orientation.Horizontal)
    settle(10)


def toolbar_button(text: str):
    """The plugin toolbar's button with that text (its action's)."""
    for bar in win.findChildren(QToolBar):
        for action in bar.actions():
            if action.text().rstrip("…") == text.rstrip("…") and bar.widgetForAction(action) is not None and bar.isVisible():
                return bar.widgetForAction(action)
    return None


def quiet_bar():
    """The message bar emptied (notices of the steps before)."""
    iface.messageBar().clearWidgets()


def modal():
    return QApplication.activeModalWidget()


# --- 1. 画像検索 → 観測記録の保存 → 担当完了 ----------------------------------------------------------


def sample_images() -> list:
    """The mock's sample images (id, time, ships) as it made them."""
    with open(os.path.join(WORK, "mock", "files", "catalog", "images.json"), encoding="utf-8") as f:
        return json.load(f)


def window_part(left: int, right: int, top: int = 0, bottom: int = 0) -> QRect:
    """A part of the main window: columns left..right (negative: from the right edge), rows top..bottom (0: to the bottom)."""
    w, h = win.width(), win.height()
    right = w + right if right <= 0 else right
    bottom = h + bottom if bottom <= 0 else bottom
    return QRect(left, top, right - left, bottom - top)


def dock_and_map(d: QDockWidget, map_px: int = 360, top: int | None = None, bottom: int | None = None) -> QRect:
    """The dock and the map beside it (from `top`: the dock's top; down to `bottom` or the dock's end), in the main window's pixels."""
    rect = QRect(d.mapTo(win, d.rect().topLeft()), d.size())
    top = rect.top() - 4 if top is None else top
    end = rect.bottom() if bottom is None else min(rect.bottom(), bottom)
    return QRect(max(0, rect.left() - map_px), top, rect.width() + map_px, end - top + 1)


def below_rows(table, extra: int = 60) -> int:
    """The window's row a little under a table's last row (a picture cut there, without the empty list)."""
    return table_rows(table, win).bottom() + extra


def hide_columns(table, columns, names) -> None:
    """Columns left out of a picture (so that those after them fit in the dock's width)."""
    for name in names:
        table.setColumnHidden(list(columns).index(name), True)
    table.resizeColumnsToContents()


#: Shown in the pictures instead of the folder the files are really written to (a temporary one).
SHOWN_FOLDER = "D:/撮像計画ファイル"


def table_row(table, row: int, owner: QWidget) -> QRect:
    """A whole row of a table view, in the owner's pixels."""
    view = table.viewport()
    first = table.visualRect(table.model().index(row, 0))
    rect = QRect(0, first.top(), view.width(), first.height())
    return moved(rect, view, owner)


def union(*rects: QRect) -> QRect:
    out = None
    for r in rects:
        out = QRect(r) if out is None else out.united(r)
    return out


def rect_in(widget: QWidget, owner: QWidget) -> QRect:
    """A widget's rectangle in the owner's pixels."""
    return moved(widget.rect(), widget, owner)


def dock_title(d: QDockWidget, owner: QWidget) -> QRect:
    """A dock's title bar (its name), in the owner's pixels."""
    r = rect_in(d, owner)
    return QRect(r.left(), r.top(), r.width(), 22)


def toolbar_picture(name: str, buttons: list) -> None:
    """The plugin's toolbar with the flow's buttons numbered under them."""
    bar = buttons[0].parentWidget()
    shown = [bar.widgetForAction(a) for a in bar.actions() if bar.widgetForAction(a) is not None and bar.widgetForAction(a).isVisible()]
    r = union(*[rect_in(w, win) for w in shown])
    shoot(path(name), win, marks=[(b, str(i + 1), "bottom") for i, b in enumerate(buttons)],
          crop=QRect(r.left() - 6, r.top() - 4, r.width() + 12, r.height() + 34))


def with_label(widget: QWidget, owner: QWidget, text: str = "") -> QRect:
    """A field and its caption on its left (the nearest label on the same line), in the owner's pixels: the numbered badge then stands left of the caption."""
    from qgis.PyQt.QtWidgets import QLabel

    field = rect_in(widget, owner)
    best = None
    for label in owner.findChildren(QLabel):
        if not label.isVisible() or not label.text() or (text and not label.text().startswith(text)):
            continue
        r = rect_in(label, owner)
        if r.right() <= field.left() + 2 and r.top() < field.bottom() and r.bottom() > field.top():
            if best is None or r.right() > best.right():
                best = r
    return field if best is None else field.united(best)


def grid_row(grid, row: int, owner: QWidget) -> QRect:
    """A row of a grid layout (its caption and its widgets), in the owner's pixels."""
    rects = []
    for c in range(grid.columnCount()):
        item = grid.itemAtPosition(row, c)
        if item is not None and item.widget() is not None and item.widget().isVisible():
            rects.append(rect_in(item.widget(), owner))
    return union(*rects)


def draw_polygon(catalog, corners) -> None:
    """The polygon of 「範囲を描く」 as it looks while drawn: the corners clicked and the line to the mouse."""
    tool = catalog.draw_tool
    tool.points = [QgsPointXY(x, y) for x, y in corners[:-1]]
    tool._show(QgsPointXY(*corners[-1]))


#: The area drawn in the picture of 「範囲を描く」 (longitude, latitude; the last one where the mouse is).
DRAWN = [(139.712, 35.262), (139.792, 35.268), (139.788, 35.336), (139.735, 35.338)]


def flow_obs():
    catalog = dock("SatObsImageCatalog")
    obs = dock("SatObsObservations")
    cat_button, obs_button = toolbar_button("画像カタログ"), toolbar_button("観測記録")
    save_button(path("btn-catalog"), cat_button)
    save_button(path("btn-obs"), obs_button)
    layout()
    canvas.setExtent(QgsRectangle(139.70, 35.25, 139.80, 35.35))
    canvas.refresh()
    yield 800
    quiet_bar()
    toolbar_picture("obs-00-toolbar", [cat_button, obs_button])
    # 1-1 画像カタログのドック.
    layout(left=[catalog], widths=(430,))
    yield 1200
    quiet_bar()
    top = rect_in(catalog, win)
    shoot(path("obs-01-dock"), win, marks=[(cat_button, "1", "bottom"), (dock_title(catalog, win), "2", "right")],
          crop=window_part(0, 760, 0, top.top() + 330))
    # 1-2 範囲: the list open.
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("extent"))
    catalog.area_box.showPopup()
    yield 600
    popup = catalog.area_box.view().window()
    form_top = global_rect(catalog.catalog_box).top() - 8
    bottom = max(global_rect(popup).bottom(), global_rect(catalog.keyword).bottom()) + 8
    whole = global_rect(catalog)
    shoot_screen(path("obs-02-area"), QRect(whole.left(), form_top, whole.width(), bottom - form_top),
                 marks=[(catalog.area_box, "1"), (global_rect(popup), "")])
    catalog.area_box.hidePopup()
    yield 300
    # 範囲を描く: the polygon while it is drawn.
    catalog.draw_button.setChecked(True)
    yield 300
    fit_left(canvas, 139.70, 35.25, 139.80, 35.35, strip=440, border=20)
    yield 800
    draw_polygon(catalog, DRAWN)
    yield 800
    left = rect_in(catalog, win)
    shoot(path("obs-03-draw"), win, marks=[(catalog.draw_button, "1"), (map_rect(canvas, 139.712, 35.262, 139.792, 35.338).translated(canvas.mapTo(win, canvas.rect().topLeft())).adjusted(-8, -8, 8, 8), "2", "top")],
          crop=window_part(0, left.right() + 440, left.top(), left.bottom()), width=WIDE)
    catalog.draw_tool.points = []
    catalog.draw_tool._show()
    catalog.draw_button.setChecked(False)
    catalog.set_drawn(None)
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("extent"))
    canvas.setExtent(QgsRectangle(139.70, 35.25, 139.80, 35.35))
    canvas.refresh()
    yield 800
    # 1-3 撮像日時.
    today = QDate.currentDate()
    catalog.acquired_from.setText(today.addDays(-7).toString("yyyy-MM-dd"))
    catalog.acquired_to.setText(today.toString("yyyy-MM-dd"))
    yield 300
    a = rect_in(catalog.area_box, catalog)
    k = rect_in(catalog.keyword, catalog)
    shoot(path("obs-04-date"), catalog, marks=[(catalog.acquired_from, "1"), (catalog.acquired_to, "2", "top")],
          crop=QRect(0, a.top() - 6, catalog.width(), k.bottom() - a.top() + 12))
    # 1-4 検索.
    catalog.search()
    yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
    catalog.table.resizeColumnsToContents()
    yield 500
    quiet_bar()
    shoot(path("obs-05-search"), catalog, marks=[(catalog.search_button, "1"), (catalog.status, "2", "top"), (table_rows(catalog.table, catalog), "3")],
          crop=[catalog.search_button, table_rows(catalog.table, catalog)], margin=6)
    # 2-1 選ぶ: the row and its footprint on the map.
    newest = max(catalog.model.records, key=lambda r: r.attributes["ACQ_TIME"])
    row = catalog.model.records.index(newest)
    from mikage.qgis_io.catalog_io import footprint
    shape = footprint(newest.geometry)
    ext = shape.boundingBox() if shape is not None else QgsRectangle(139.72, 35.27, 139.78, 35.33)
    fit_left(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum(), strip=440, border=40)
    catalog.table.selectRow(row)
    yield 1500
    quiet_bar()
    on_map = map_rect(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()).translated(canvas.mapTo(win, canvas.rect().topLeft()))
    shoot(path("obs-06-select"), win, marks=[(table_row(catalog.table, row, win), "1"), (on_map.adjusted(-6, -6, 6, 6), "2", "top")],
          crop=window_part(0, left.right() + 440, left.top(), left.bottom()), width=WIDE)
    # 2-2 開く.
    shoot(path("obs-07-open"), catalog, marks=[(catalog.buttons["open"], "1"), (catalog.close_previous, ""), (catalog.zoom_opened, "")],
          crop=[catalog.buttons["open"], catalog.zoom_opened], margin=6)
    catalog.open_records([newest])
    yield lambda: any(isinstance(l, QgsRasterLayer) for l in QgsProject.instance().mapLayers().values())
    yield 2500
    fit_left(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum(), strip=440, border=40)
    yield 2000
    quiet_bar()
    shoot(path("obs-08-opened"), win, crop=window_part(0, left.right() + 440, left.top(), left.bottom()), width=WIDE)
    image_id = newest.id
    log(f"opened {image_id}")
    # 3 観測記録ドック: the store, the image, the 分担 line. The map wider, the catalog's work done.
    layout(right=[obs], widths=(OBS_WIDTH,))
    if obs.store_select.findText("モック観測記録") >= 0:
        obs.store_select.setCurrentIndex(obs.store_select.findText("モック観測記録"))
    yield lambda: obs.book is not None and obs.book.loaded and obs.book.current is not None
    # 分担 read: everyone's, or only this image's rows (since 2026-11-19).
    yield lambda: obs.assignment_bar.read_at > 0 or bool(obs.assignment_bar._image_read_at)
    ships = next(p for p in sample_images() if p["id"] == image_id)["ships"]
    lons, lats = [s["lon"] for s in ships], [s["lat"] for s in ships]
    ship_area = (min(lons) - 0.004, min(lats) - 0.004, max(lons) + 0.004, max(lats) + 0.004)
    fit_right(canvas, *ship_area, strip=OBS_MAP, border=70)
    yield 1500
    quiet_bar()
    shoot(path("obs-09-dock"), win, marks=[(obs.store_select, "1"), (obs.image_select, "2"), (obs.assignment_bar, "3")],
          crop=dock_and_map(obs, OBS_MAP, bottom=below_rows(obs.table, 180)), width=WIDE)
    # 4 クリック記録: the image is taken (分担) first.
    obs.record_button.setChecked(True)
    yield lambda: obs.active
    yield lambda: obs.assignment_bar.read_at > 0 and obs.assignment_bar.done_button.isVisible()
    yield 500
    quiet_bar()
    shoot(path("obs-10-record"), obs, marks=[(obs.record_button, "1"), (obs.assignment_bar.label, "2")],
          crop=[obs.store_select, obs.record_button, obs.status], margin=6)
    # 7-3 (shot now, while the marks of the image before are all there): すべて継続・すべて消失.
    shoot(path("obs-16-all"), obs, marks=[(obs.carry_label, "1"), (obs.carry_all_button, "2", "top"), (obs.absent_all_button, "3", "top")],
          crop=[obs.record_button, obs.carry_label, obs.absent_all_button], margin=(6, 30))
    # 5 Close to the ship to record.
    new = next(s for s in ships if s["n"] == 2)
    d = 0.007
    canvas.setExtent(QgsRectangle(new["lon"] - d * 1.4, new["lat"] - d, new["lon"] + d * 1.4, new["lat"] + d))
    canvas.refresh()
    yield 1500
    k = 0.0011
    near = map_rect(canvas, new["lon"] - k, new["lat"] - k, new["lon"] + k, new["lat"] + k)
    shoot(path("obs-11-click"), canvas, marks=[(near, "1")], crop=near.adjusted(-260, -150, 260, 150), pad=0)
    obs.map_clicked(new["lon"], new["lat"], None)
    yield lambda: obs.form.isVisible()
    yield 500
    # 6-1 種別: 大分類 → 種別 → 級.
    obs.choose_kind(["船舶", "貨物船"])
    yield 500
    grid = obs.kind_levels.grid
    marks = [(grid_row(grid, r, obs.form), str(r + 1)) for r in range(min(3, grid.rowCount()))] if grid is not None else []
    shoot(path("obs-12-kind"), obs.form, marks=marks + [(obs.kind_input, "")],
          crop=[obs.object_input, obs.kind_input, obs.kind_path_box], margin=(8, 6))
    # 6-2 活動・確度.
    obs._pick_activity("停泊")
    obs.confidence_select.setCurrentIndex(max(0, obs.confidence_select.findData("推定")))
    yield 500
    shoot(path("obs-13-activity"), obs.form, marks=[(obs.chips_box, "1"), (obs.activity_input, "2"), (obs.confidence_select, "3")],
          crop=[obs.chips_box, obs.confidence_select], margin=(8, 6))
    # 6-3 備考・記録.
    obs.note_input.setText("岸壁の沖で停泊")
    yield 500
    shoot(path("obs-14-submit"), obs.form, marks=[(obs.note_input, "1"), (obs.submit_button, "2")],
          crop=[obs.note_input, obs.submit_button], margin=(8, 8))
    obs.submit()
    yield lambda: not obs.form.isVisible()
    yield 1000
    # こうなれば OK: on the map and in the list.
    fit_right(canvas, *ship_area, strip=OBS_MAP, border=70)
    yield 1800
    quiet_bar()
    mine = obs.table.model().rowCount()
    kept = None
    for r in range(mine):
        if "岸壁の沖" in " ".join(str(obs.table.model().index(r, c).data() or "") for c in range(obs.table.model().columnCount())):
            kept = r
    k2 = 0.0016
    spot = map_rect(canvas, new["lon"] - k2, new["lat"] - k2, new["lon"] + k2, new["lat"] + k2).translated(canvas.mapTo(win, canvas.rect().topLeft()))
    shoot(path("obs-15-recorded"), win, marks=[(spot, "1")] + ([(table_row(obs.table, kept, win), "2")] if kept is not None else []),
          crop=dock_and_map(obs, OBS_MAP, bottom=below_rows(obs.table, 180)), width=WIDE)
    # 7 前の画像にいたモノ (白抜きの印): click it, 継続.
    ghost = next((m for m in obs.scene().marks if m.role == "ghost"), None)
    if ghost is not None:
        canvas.setExtent(QgsRectangle(ghost.lon - d * 1.4, ghost.lat - d, ghost.lon + d * 1.4, ghost.lat + d))
        canvas.refresh()
        yield 1500
        near = map_rect(canvas, ghost.lon - k, ghost.lat - k, ghost.lon + k, ghost.lat + k)
        shoot(path("obs-17-ghost"), canvas, marks=[(near, "1")], crop=near.adjusted(-260, -150, 260, 150), pad=0)
        obs.map_clicked(ghost.lon, ghost.lat, ghost)
        yield lambda: obs.form.isVisible()
        obs._pick_activity("停泊")
        yield 500
        shoot(path("obs-18-carry"), obs.form, marks=[(obs.submit_button, "1"), (obs.absent_button, "2", "top")],
              crop=[obs.note_input, obs.submit_button, obs.absent_button], margin=(8, 30))
        obs.submit()
        yield lambda: not obs.form.isVisible()
        yield 1000
    else:
        log("no ghost: obs-17/18 not taken")
    # 8 記録を確かめる: the rows recorded and the one of the image before still to look at (grey).
    fit_right(canvas, *ship_area, strip=OBS_MAP, border=70)
    yield 2000
    quiet_bar()
    model = obs.table.model()
    grey = [r for r in range(model.rowCount()) if "前の画像" in " ".join(str(model.index(r, c).data() or "") for c in range(model.columnCount()))]
    done = [r for r in range(model.rowCount()) if r not in grey]
    marks = []
    if done:
        marks.append((union(*[table_row(obs.table, r, win) for r in done]), "1"))
    if grey:
        marks.append((union(*[table_row(obs.table, r, win) for r in grey]), "2"))
    shoot(path("obs-19-list"), win, marks=marks + [(obs.record_button, "3")], crop=dock_and_map(obs, OBS_MAP, bottom=below_rows(obs.table, 180)), width=WIDE)
    log(f"obs rows: done {done} grey {grey}")
    obs.record_button.setChecked(False)
    yield 500
    # 9 完了.
    shoot(path("obs-20-done-before"), obs, marks=[(obs.assignment_bar.done_button, "1")], crop=[obs.assignment_bar], margin=(4, 5))
    obs.assignment_bar.done_button.click()
    yield lambda: "完了" in obs.assignment_bar.label.text()
    yield 500
    shoot(path("obs-21-done-after"), obs, marks=[(obs.assignment_bar.label, "")], crop=[obs.assignment_bar], margin=(4, 5))
    layout(left=[catalog], right=[obs], widths=(430, OBS_WIDTH))
    catalog.search()
    yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
    yield 1500
    catalog.table.resizeColumnsToContents()
    yield 300
    shoot(path("obs-22-catalog-done"), catalog, marks=[(table_row(catalog.table, 0, catalog), "1")],
          crop=[table_rows(catalog.table, catalog)], margin=4)
    # 9-3 次の画像.
    layout(right=[obs], widths=(OBS_WIDTH,))
    yield 800
    shoot(path("obs-23-next"), obs, marks=[(obs.prev_button, "1", "top"), (obs.image_select, "2", "top"), (obs.next_button, "3", "top")],
          crop=[obs.prev_button, obs.next_button], margin=(8, 30))
    log("obs labels: " + obs.assignment_bar.label.text())
    # The images opened are not part of the next steps.
    for layer in list(QgsProject.instance().mapLayers().values()):
        if isinstance(layer, QgsRasterLayer) and layer.name() != BASEMAP_NAME:
            QgsProject.instance().removeMapLayer(layer.id())


# --- 2. 撮像依頼 (依頼元) → 3. 計算・書き出し・採否 (計画の担当) → 2. 計画の確認 (依頼元) ----------------------------

#: The requester's request (its name), and its area (west, south, east, north).
MINE = "横須賀沖 岸壁（臨時）"
MINE_AREA = (139.738, 35.302, 139.764, 35.322)
EXPORTS = os.environ.get("EXPORTS", WORK)


#: The day planned: the first one from tomorrow on where the requester's request gets a pass (the mock's orbits
#: start from the time it was started, so the passes move from one run to the next).
TARGET = [QDate.currentDate().addDays(1)]


def tomorrow() -> QDate:
    return TARGET[0]


def clear_bar_and_settle():
    quiet_bar()
    settle(10)


def popup_item(combo, text: str) -> QRect | None:
    """The screen rectangle of the open list's row whose text starts with `text`."""
    view = combo.view()
    model = view.model()
    rows = [model.index(r, 0) for r in range(model.rowCount())]
    found = [i for i in rows if str(i.data() or "") == text] or [i for i in rows if str(i.data() or "").startswith(text)]
    if not found:
        return None
    rect = view.visualRect(found[0])
    return QRect(view.viewport().mapToGlobal(rect.topLeft()), rect.size())


def shoot_popup(name: str, combo, owner: QWidget, top_widget: QWidget, marks: list, below: int = 8) -> None:
    """A drop-down list open under its box: the owner's width, from `top_widget` down to the list's end."""
    combo.showPopup()
    settle(10)
    popup = combo.view().window()
    top = min(global_rect(top_widget).top(), global_rect(popup).top()) - 8
    whole = global_rect(owner)
    left = min(whole.left(), global_rect(popup).left() - 4)
    right = max(whole.right(), global_rect(popup).right() + 4)
    bottom = global_rect(popup).bottom() + below
    shoot_screen(path(name), QRect(left, top, right - left, bottom - top), marks=[m(popup) if callable(m) else m for m in marks])
    combo.hidePopup()
    settle(5)


def form_rows(form, first: QWidget, last: QWidget, extra: int = 6) -> QRect:
    """The form's rows from `first` to `last`, its whole width, in the form's pixels."""
    a, b = rect_in(first, form), rect_in(last, form)
    return QRect(0, a.top() - extra, form.width(), b.bottom() - a.top() + 2 * extra)


def flow_request():
    requests = dock("SatObsRequests")
    buttons = [toolbar_button("撮像依頼"), toolbar_button("撮像予定"), toolbar_button("地点ごとの撮像予定")]
    for name, b in zip(("btn-request", "btn-schedule", "btn-places"), buttons):
        save_button(path(name), b)
    layout()
    yield 500
    quiet_bar()
    toolbar_picture("req-00-toolbar", buttons)
    layout(right=[requests], widths=(620,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: requests.store is not None and requests.task is None and bool(requests.requests)
    yield 1500
    quiet_bar()
    # 1-1 the dock and its list.
    under = rect_in(requests.export_button, win).bottom() + 6
    shoot(path("req-01-dock"), win, marks=[(dock_title(requests, win), "1"), (table_rows(requests.table, win), "2")],
          crop=dock_and_map(requests, PLAN_MAP, bottom=under), width=WIDE)
    # 1-2 自分のグループ.
    requests.group_filter.choose("*mine*")  # 自分のグループ (core/request/groups.MINE)
    yield 800
    shoot_popup("req-02-group", requests.group_filter, requests, requests.status_filter,
                [(requests.group_filter, "1"), lambda popup: (popup_item(requests.group_filter, "自分のグループ") or global_rect(popup), "2")])
    yield 500
    # 1-3 新規….
    shoot(path("req-03-new"), requests, marks=[(requests.new_button, "1")], crop=[requests.new_button, requests.reload_button, requests.export_button], margin=(8, 6))
    requests.edit(new=True)
    form = requests.form
    yield lambda: form is not None and form.isVisible()
    form.resize(600, 1040)
    form.move(win.x() + 150, 5)
    form.name.setText(MINE)
    if form.group.findText("第1班") >= 0:
        form.group.setCurrentIndex(form.group.findText("第1班"))
    yield 500
    # 2-1 名称・番号・グループ.
    shoot(path("req-04-name"), form, marks=[(with_label(form.name, form), "1"), (union(with_label(form.management_no, form), rect_in(form.identification_no, form)), "2"),
                                            (with_label(form.group, form), "3")], crop=form_rows(form, form.name, form.group))
    # 2-2 区分・期間.
    form.category.setCurrentText("臨時")
    form.achieve.setCurrentText("期間内1回")
    form.start.setDate(QDate.currentDate())
    form.end.setDate(QDate.currentDate().addDays(7))
    yield 500
    shoot(path("req-05-when"), form, marks=[(with_label(form.category, form), "1"), (with_label(form.achieve, form), "2", "top"),
                                            (union(with_label(form.start, form), rect_in(form.end, form)), "3")],
          crop=form_rows(form, form.category, form.times_min).adjusted(0, -22, 0, 0))
    # 2-3 優先度: the list open.
    high = form.level.findText("高", Qt.MatchFlag.MatchStartsWith)
    if high >= 0:
        form.level.setCurrentIndex(high)
        form.priority.setValue(form.level.itemData(high))
    yield 300
    shoot_popup("req-06-level", form.level, form, form.level, [(form.level, "1"), lambda popup: (global_rect(popup), "")])
    # 2-4 条件.
    form.kind.setCurrentText("光学")
    yield 500
    cond = form.kind.parentWidget()
    while cond is not None and not cond.inherits("QGroupBox"):
        cond = cond.parentWidget()
    shoot(path("req-07-cond"), form, marks=[(with_label(form.kind, form), "1"), (form.tree, "2"),
                                            (union(with_label(form.ona_min, form), rect_in(form.inc_max, form), with_label(form.resolution, form), rect_in(form.cloud, form), with_label(form.systems, form)), "3")],
          crop=rect_in(cond, form).adjusted(-4, -4, 4, 4) if cond is not None else None)
    # 2-5 範囲.
    w, south, e, n = MINE_AREA
    form._extent_drawn(QgsRectangle(w, south, e, n))
    yield lambda: form.chances_label.isVisible() and form.chances_label.text() != ""
    yield 1500
    area = form.box_button.parentWidget()
    while area is not None and not area.inherits("QGroupBox"):
        area = area.parentWidget()
    shoot(path("req-08-area"), form, marks=[(form.box_button, "1"), (union(rect_in(form.polygon_button, form), rect_in(form.selected_button, form)), "2", "top"),
                                            (form.chances_label, "3")],
          crop=rect_in(area, form).adjusted(-4, -4, 4, 4) if area is not None else None)
    canvas.setExtent(QgsRectangle(139.715, 35.288, 139.787, 35.336))
    canvas.refresh()
    yield 1500
    on_map = map_rect(canvas, w, south, e, n)
    shoot(path("req-09-area-map"), canvas, marks=[(on_map.adjusted(-6, -6, 6, 6), "1")], width=560)
    # 2-6 依頼者・メモ・保存.
    form.requester.setText("田中（第1班）")
    form.note.setPlainText("岸壁の船の入れ替わりを確認したい")
    yield 500
    box = form.findChildren(QDialogButtonBox)[0]
    save = box.button(QDialogButtonBox.StandardButton.Save)
    shoot(path("req-10-save"), form, marks=[(with_label(form.requester, form), "1"), (with_label(form.note, form), "2"), (save, "3")],
          crop=QRect(0, rect_in(form.requester, form).top() - 8, form.width(), rect_in(box, form).bottom() - rect_in(form.requester, form).top() + 14))
    save.click()
    yield lambda: not form.isVisible()
    yield lambda: any(r.name == MINE for r in requests.shown)
    yield 1200
    row = next(i for i, r in enumerate(requests.shown) if r.name == MINE)
    requests.table.selectRow(row)
    hide_columns(requests.table, [requests.model.headerData(c, Qt.Orientation.Horizontal) for c in range(requests.model.columnCount())],
                 ("管理番号", "識別番号"))
    yield 800
    shoot(path("req-11-saved"), requests, marks=[(table_row(requests.table, row, requests), "1"), (requests.edit_button, "2")],
          crop=[requests.status, table_rows(requests.table, requests), requests.edit_button], margin=4)
    log(f"requests shown {len(requests.shown)} mine row {row}")



#: The columns of 「撮像計画ファイル出力」 left out of its pictures, so that 結果・理由 shows in the dock's width.
PLAN_HIDDEN = ("システム", "依頼グループ", "区分", "雲量", "枚数", "繰り上げ")
#: The same for 「撮像予定」.
SCHEDULE_HIDDEN = ("管理番号", "システム", "撮像", "依頼者")


def column_cells(table, column: int, owner: QWidget) -> QRect:
    """A column of a table view (its header and rows), in the owner's pixels."""
    header = table.horizontalHeader()
    x = header.sectionViewportPosition(column)
    width = header.sectionSize(column)
    rows = table_rows(table, owner)
    left = moved(QRect(x, 0, width, 1), table.viewport(), owner).left()
    return QRect(left, rows.top(), width, rows.height())


def flow_plan():
    from mikage.ui.plan_file_dock import COLUMNS

    files = dock("SatObsPlanFiles")
    schedule = dock("SatObsSchedule")
    buttons = [toolbar_button("撮像計画ファイル出力"), toolbar_button("撮像予定")]
    for name, b in zip(("btn-plan-files", "btn-schedule"), buttons):
        save_button(path(name), b)
    layout()
    yield 500
    quiet_bar()
    toolbar_picture("plan-00-toolbar", buttons)
    layout(right=[files], widths=(780,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: files.store is not None
    from mikage.core.request.daily import TAKEN

    files.day.setDate(QDate.currentDate().addDays(1))
    yield 500
    # 1-1 対象日・計算 (before the first run).
    shoot(path("plan-01-day"), files, marks=[(with_label(files.day, files), "1"), (files.run_button, "2", "top")],
          crop=union(with_label(files.day, files), rect_in(files.quota_button, files)).adjusted(-38, -30, 8, 8))
    for ahead in range(1, 8):
        files.day.setDate(QDate.currentDate().addDays(ahead))
        files.plan = None
        yield 300
        files.run()
        yield lambda: files.plan is not None and files.task is None
        if any(c.request.name == MINE and c.result == TAKEN for c in files.plan.candidates):
            break
    TARGET[0] = files.day.date()
    log(f"target day {TARGET[0].toString('yyyy-MM-dd')}")
    hide_columns(files.table, COLUMNS, PLAN_HIDDEN)
    yield 2000
    quiet_bar()
    log(f"plan candidates {len(files.plan.candidates)} status {files.status.text()}")
    model = files.table.model()
    for r in range(model.rowCount()):
        log("  " + " | ".join(str(model.index(r, c).data() or "") for c in range(model.columnCount())))
    # 1-2 結果: the status line and the list.
    under = rect_in(files.on_map, win).bottom() + 8
    shoot(path("plan-02-calc"), win, marks=[(files.status, "1"), (table_rows(files.table, win), "2")],
          crop=dock_and_map(files, PLAN_MAP, bottom=under), width=WIDE)
    # 2-1 一覧: 採用 column.
    shoot(path("plan-03-table"), files, marks=[(column_cells(files.table, COLUMNS.index("採用"), files), "1", "top")],
          crop=[files.status, table_rows(files.table, files)], margin=(4, 30))
    # 2-2 地図に表示.
    files.on_map.setChecked(True)
    files.draw()
    yield 500
    shoot(path("plan-04-on-map"), files, marks=[(files.on_map, "1")], crop=[files.on_map], margin=(260, 8))
    canvas.setExtent(QgsRectangle(139.55, 35.17, 139.95, 35.43))
    canvas.refresh()
    yield 2000
    shoot(path("plan-05-map"), canvas, width=WIDE)
    # 3 書き出し: the files of each system, after asking (the folder shown as a user's).
    files.folder.setText(SHOWN_FOLDER)
    row = moved(files.folder.rect(), files.folder, files).united(moved(files.write_button.rect(), files.write_button, files))
    shoot(path("plan-06-write"), files, marks=[(files.folder, "1", "bottom"), (files.write_button, "2", "top")],
          crop=QRect(0, row.top() - 30, files.width(), row.height() + 60))
    files.folder.setText(EXPORTS)
    times = {str(model.index(r, COLUMNS.index("依頼")).data()): str(model.index(r, COLUMNS.index("撮像時刻")).data() or "") for r in range(model.rowCount())}
    later(lambda: files.write(ask=True))
    yield lambda: isinstance(modal(), QMessageBox)
    yield 500
    box = modal()
    box.setText(box.text().replace(EXPORTS.replace("\\", "/"), SHOWN_FOLDER).replace(EXPORTS, SHOWN_FOLDER))
    box.adjustSize()
    settle(5)
    yes = go_button(box)
    shoot(path("plan-07-confirm"), box, marks=[(yes, "1")])
    yes.click()
    yield lambda: modal() is None
    yield lambda: files.task is None
    yield 2500
    quiet_bar()
    shoot(path("plan-08-written"), files, marks=[(files.status, "1")], crop=[files.status, table_rows(files.table, files)], margin=4)
    log("written: " + ", ".join(sorted(os.listdir(EXPORTS))))
    # 4 採否: the optical system answers in its file; the other one is told by phone (by hand).
    answer = write_answer(times)
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: schedule.store is not None
    schedule.group_filter.choose([])
    schedule.set_day(tomorrow().toPyDate())
    yield lambda: schedule.task is None and schedule.day is not None
    yield 1500
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 500
    quiet_bar()
    under = rect_in(schedule.import_button, win).bottom() + 8
    shoot(path("plan-09-schedule"), win, marks=[(schedule.calendar, "1"), (table_rows(schedule.table, win), "2"), (schedule.import_button, "3")],
          crop=dock_and_map(schedule, PLAN_MAP, bottom=under), width=WIDE)
    later(lambda: schedule.import_file(path=answer, ask=True))
    yield lambda: isinstance(modal(), QMessageBox)
    yield 500
    box = modal()
    yes = go_button(box)
    shoot(path("plan-10-import"), box, marks=[(yes, "1")])
    yes.click()
    yield lambda: modal() is None
    yield lambda: schedule.task is None
    yield 2500
    log(f"schedule rows {[(r.name, getattr(r, 'adoption', '')) for r in schedule.shown]}")
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 500
    taken = [i for i, r in enumerate(schedule.shown) if getattr(r, "adoption", "") == "採用"]
    waiting = [i for i, r in enumerate(schedule.shown) if r.name == BY_PHONE and getattr(r, "adoption", "") == "不明"]
    marks = []
    if waiting:
        marks.append((table_row(schedule.table, waiting[0], schedule), "1"))
    if taken:
        marks.append((union(*[table_row(schedule.table, i, schedule) for i in taken]), "2"))
    shoot(path("plan-11-imported"), schedule, marks=marks, crop=[schedule.status, table_rows(schedule.table, schedule)], margin=4)
    # 5 手入力.
    if waiting:
        schedule.table.selectRow(waiting[0])
        schedule.reason.setText("電話で確認（競合）")
        schedule.reason.setCursorPosition(0)
        yield 500
        shoot(path("plan-12-by-hand"), schedule, marks=[(table_row(schedule.table, waiting[0], schedule), "1"), (schedule.reason, "2", "top"),
                                                         (schedule.reject_button, "3", "top")],
              crop=[schedule.table, schedule.adopt_button, schedule.reason], margin=4)
        schedule.answer("不採用")
        yield lambda: schedule.task is None
        yield 2000
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 500
    rejected = [i for i, r in enumerate(schedule.shown) if r.name == BY_PHONE]
    if rejected:
        schedule.table.selectRow(rejected[0])
        yield 500
    shoot(path("plan-13-answered"), schedule, marks=([(table_row(schedule.table, rejected[0], schedule), "1")] if rejected else []) +
          [(schedule.wait_button, "2", "top")],
          crop=[schedule.status, table_rows(schedule.table, schedule), schedule.adopt_button, schedule.reason], margin=4)


def go_button(box: QMessageBox):
    """The button that goes on: はい, or the action's own button (「取り込み」 and the like, ui/docks.ask_no with `yes`)."""
    yes = box.button(QMessageBox.StandardButton.Yes)
    if yes is not None:
        return yes
    return next(b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole)


#: 「撮像予定」 wide enough for its calendar and the columns shown.
SCHEDULE_WIDTH = 800


def schedule_columns():
    from mikage.ui.schedule_dock import COLUMNS

    return COLUMNS


#: The request whose answer comes by phone (put in by hand); the system's file answers the others.
BY_PHONE = "木更津 臨時"


def write_answer(times: dict) -> str:
    """The optical system's answer to the file written, as the mock's system would send it back: all taken but one."""
    import csv
    written = sorted((os.path.join(EXPORTS, n) for n in os.listdir(EXPORTS) if n.lower().endswith(".csv")), key=os.path.getmtime)
    rows = []
    if written:
        with open(written[-1], encoding="cp932", newline="") as f:
            rows = list(csv.DictReader(f))
    out = os.path.join(WORK, "MOCK-OPT_回答_" + tomorrow().toString("yyyyMMdd") + ".csv")
    day = tomorrow().toString("yyyy/MM/dd")
    with open(out, "w", encoding="cp932", newline="") as f:
        w = csv.writer(f)
        w.writerow(["依頼ID", "判定", "理由", "撮像予定"])
        for r in rows:
            name = r.get("名称", "")
            if name and name != BY_PHONE:
                w.writerow([r.get("依頼ID", ""), "○", "", f"{day} {times.get(name) or '10:30'}"])
    with open(out, encoding="cp932") as f:
        log(f"answer from {written[-1] if written else None}: {f.read()}")
    return out


def calendar_cell(calendar, day: QDate, owner: QWidget) -> QRect | None:
    """The day's cell in a calendar widget (its month shown), in the owner's pixels."""
    from qgis.PyQt.QtWidgets import QTableView

    view = calendar.findChild(QTableView)
    if view is None:
        return None
    model = view.model()
    hits = []
    for r in range(1, model.rowCount()):
        for c in range(model.columnCount()):
            if str(model.index(r, c).data() or "") == str(day.day()):
                hits.append(model.index(r, c))
    if not hits:
        return None
    # A day number can show twice (the month before or after, in grey): the first for early days, the last for late ones.
    index = hits[0] if day.day() < 15 else hits[-1]
    return moved(view.visualRect(index), view.viewport(), owner)


def flow_check():
    schedule = dock("SatObsSchedule")
    places = dock("SatObsPlaceSchedule")
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: schedule.store is not None
    schedule.set_day(tomorrow().toPyDate())
    yield lambda: schedule.task is None
    yield 1000
    schedule.table.clearSelection()
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 1000
    quiet_bar()
    # 5-1 the dock: calendar and list.
    under = below_rows(schedule.table, 40)
    shoot(path("check-01-dock"), win, marks=[(schedule.calendar, "1"), (table_rows(schedule.table, win), "2")],
          crop=dock_and_map(schedule, PLAN_MAP, bottom=under), width=WIDE)
    # 5-2 the calendar: today chosen for the picture, so that the day planned shows its colour.
    schedule.calendar.blockSignals(True)
    schedule.calendar.setSelectedDate(QDate.currentDate())
    settle(10)
    cell = calendar_cell(schedule.calendar, tomorrow(), schedule)
    shoot(path("check-02-calendar"), schedule, marks=[(cell, "1", "top")] if cell is not None else [], crop=[schedule.calendar], margin=(2, 2))
    schedule.calendar.setSelectedDate(tomorrow())
    schedule.calendar.blockSignals(False)
    # 5-3 自分のグループ.
    schedule.group_filter.choose("*mine*")  # 自分のグループ (core/request/groups.MINE)
    yield 1000
    shoot_popup("check-03-group", schedule.group_filter, schedule, schedule.group_filter,
                [(schedule.group_filter, "1"), lambda popup: (popup_item(schedule.group_filter, "自分のグループ") or global_rect(popup), "2")])
    mine = [i for i, r in enumerate(schedule.shown) if r.name == MINE]
    if mine:
        schedule.table.selectRow(mine[0])
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 1500
    # Choosing the row moved the map to the request: back into the part of the map shown.
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield 1000
    quiet_bar()
    # 6 the row.
    shoot(path("check-04-row"), schedule, marks=[(table_row(schedule.table, mine[0], schedule), "1")] if mine else [],
          crop=[schedule.status, table_rows(schedule.table, schedule)], margin=4)
    # 6 見送り: the states' list open.
    shoot_popup("check-05-state", schedule.state_filter, schedule, schedule.state_filter,
                [(schedule.state_filter, "1"), lambda popup: (popup_item(schedule.state_filter, "見送り") or global_rect(popup), "2")])
    # 7 地点ごとの撮像予定: the request's place on a timeline.
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,), bottom=[places])
    win.resizeDocks([places], [300], Qt.Orientation.Vertical)
    w, south, e, n = MINE_AREA
    places.add_point((w + e) / 2, (south + n) / 2, "岸壁の沖")
    yield lambda: getattr(places, "task", None) is None
    yield 2500
    shoot(path("check-06-places"), places, marks=[(places.pick, "1", "top"), (places.timeline, "2")])
    log("check done")


# --- 2'. たくさんの場所をまとめて依頼する（レイヤーから取り込み）. Last: its requests would change the plan's pictures. ---------

#: The places of the requester's layer: name, 管理番号, outline (lon, lat).
PLACES = [
    ("北の岸壁", "K-101", [(139.736, 35.331), (139.757, 35.329), (139.760, 35.341), (139.744, 35.346), (139.734, 35.340)]),
    ("中の岸壁", "K-102", [(139.768, 35.300), (139.790, 35.298), (139.792, 35.314), (139.771, 35.316)]),
    ("南の錨地", "K-103", [(139.735, 35.262), (139.761, 35.258), (139.766, 35.276), (139.748, 35.284), (139.733, 35.276)]),
]


def places_layer() -> QgsVectorLayer:
    """The requester's own layer of places to watch (a scratch layer with a name and a number)."""
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "見たい岸壁", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("名称", QVariant.String), QgsField("管理番号", QVariant.String)])
    layer.updateFields()
    for name, number, ring in PLACES:
        f = QgsFeature(layer.fields())
        f.setAttributes([name, number])
        f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring + ring[:1]]]))
        provider.addFeature(f)
    layer.updateExtents()
    symbol = layer.renderer().symbol()
    symbol.setColor(QColor(124, 58, 237, 60))
    symbol.symbolLayer(0).setStrokeColor(QColor("#7c3aed"))
    symbol.symbolLayer(0).setStrokeWidth(0.6)
    label = QgsPalLayerSettings()
    label.fieldName = "名称"
    text = QgsTextFormat()
    text.setSize(9)
    text.buffer().setEnabled(True)
    label.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(label))
    layer.setLabelsEnabled(True)
    QgsProject.instance().addMapLayer(layer)
    return layer


def flow_layer():
    requests = dock("SatObsRequests")
    layout(right=[requests], widths=(620,))
    yield lambda: requests.store is not None and requests.task is None and bool(requests.requests)
    layer = places_layer()
    iface.setActiveLayer(layer)
    xs = [x for _n, _k, ring in PLACES for x, _y in ring]
    ys = [y for _n, _k, ring in PLACES for _x, y in ring]
    yield 500
    fit_right(canvas, min(xs), min(ys), max(xs), max(ys), strip=PLAN_MAP, border=40)
    yield 1500
    quiet_bar()
    shoot(path("imp-01-layer"), win, marks=[(requests.import_button, "1", "top")], crop=dock_and_map(requests, PLAN_MAP), width=WIDE)
    dialog = requests.import_layer(layer)
    yield lambda: dialog.isVisible()
    dialog.point_km.setValue(1.0)
    yield 500
    shoot(path("imp-02-choose"), dialog, marks=[(with_label(dialog.layer, dialog), "1"), (with_label(dialog.name_field, dialog), "2"),
                                                (with_label(dialog.management_field, dialog), "3")])
    shoot(path("imp-03-next"), dialog, marks=[(dialog.selected_only, "1"), (with_label(dialog.point_km, dialog), "2"), (dialog.simple, "3"), (dialog.ok, "4")])
    dialog.ok.click()
    yield lambda: requests.form is not None and requests.form.isVisible() and requests.form.batch is not None
    form = requests.form
    form.resize(600, 1040)
    form.move(win.x() + 150, 5)
    if form.group.findText("第1班") >= 0:
        form.group.setCurrentIndex(form.group.findText("第1班"))
    form.start.setDate(QDate.currentDate())
    form.end.setDate(QDate.currentDate().addDays(30))
    middle = form.level.findText("中", Qt.MatchFlag.MatchStartsWith)
    if middle >= 0:
        form.level.setCurrentIndex(middle)
        form.priority.setValue(form.level.itemData(middle))
    form.kind.setCurrentText("光学")
    form.requester.setText("田中（第1班）")
    yield 1500
    shoot(path("imp-04-form"), form, marks=[(with_label(form.name, form), "1"), (with_label(form.category, form), "2"),
                                            (union(with_label(form.start, form), rect_in(form.end, form)), "3")],
          crop=form_rows(form, form.name, form.level))
    box = form.findChildren(QDialogButtonBox)[0]
    save = box.button(QDialogButtonBox.StandardButton.Save)
    a = rect_in(form.area_label, form)
    shoot(path("imp-05-area"), form, marks=[(form.area_label, "1"), (save, "2")],
          crop=QRect(0, a.top() - 40, form.width(), rect_in(box, form).bottom() - a.top() + 46))
    # The areas taken in, in orange on the map (behind the form).
    shoot(path("imp-06-map"), canvas, width=560)
    save.click()
    yield lambda: not form.isVisible()
    yield lambda: requests.task is None and sum(r.name in [p[0] for p in PLACES] for r in requests.shown) == len(PLACES)
    yield 1500
    rows = [i for i, r in enumerate(requests.shown) if r.name in [p[0] for p in PLACES]]
    requests.table.clearSelection()
    for i in rows:
        requests.table.selectionModel().select(requests.model.index(i, 0), requests.table.selectionModel().SelectionFlag.Select
                                                | requests.table.selectionModel().SelectionFlag.Rows)
    hide_columns(requests.table, [requests.model.headerData(c, Qt.Orientation.Horizontal) for c in range(requests.model.columnCount())],
                 ("識別番号",))
    yield 800
    shoot(path("imp-07-saved"), requests, marks=[(requests.status, "1"), (union(*[table_row(requests.table, i, requests) for i in rows]), "2"),
                                                 (requests.bulk_button, "3")],
          crop=[requests.status, table_rows(requests.table, requests), requests.bulk_button], margin=4)
    log(f"imported: {requests.status.text()}")

SCENARIOS = {"obs": flow_obs, "request": flow_request, "plan": flow_plan, "check": flow_check, "layer": flow_layer}


def set_role(name: str) -> None:
    """The role (担当) ticked alone, as in 「担当と個人設定」: its menus and toolbar buttons (and the mock's table)."""
    from mikage.core import roles

    plugin().role_keeper.choose(roles.codes_of([name]))
    settle(5)


ROLES = {"obs": "観測記録の担当", "request": "撮像を依頼する人", "plan": "撮像計画の担当", "check": "撮像を依頼する人", "layer": "撮像を依頼する人"}


def add_basemap() -> None:
    background = os.environ.get("BASEMAP")
    if background and os.path.exists(background):
        layer = QgsRasterLayer(background, BASEMAP_NAME)
        QgsProject.instance().addMapLayer(layer, False)
        QgsProject.instance().layerTreeRoot().insertLayer(-1, layer)


def everything():
    win.move(0, 0)
    # The sample 関心地域・地点 layers (added at start) are not part of these steps.
    for layer in list(QgsProject.instance().mapLayers().values()):
        if layer.name() in ("関心地域", "関心地点"):
            QgsProject.instance().removeMapLayer(layer.id())
    add_basemap()
    for name in FLOWS:
        log(f"--- {name}")
        win.resize(*WINDOW[name])
        settle(10)
        set_role(ROLES[name])
        log("toolbar: " + "・".join(a.text() for a in plugin().toolbar.actions()) if hasattr(plugin(), "toolbar") else "")
        yield from SCENARIOS[name]()
        yield 300


def finished():
    log("done")
    LOG.close()
    # Without 「保存しますか」 for the project changed on the way.
    later(lambda: os._exit(0), 300)


#: Kept here: Qt holds the runner's bound methods weakly.
RUNNER = Runner(everything(), log, finished)
# The plugin reads its settings file and preloads after QGIS starts.
later(RUNNER.start, 5000)
