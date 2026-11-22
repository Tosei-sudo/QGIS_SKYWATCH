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
WINDOW = {"sample": (1600, 1000), "obs": (1600, 1000), "request": (1600, 800), "plan": (1600, 800), "check": (1600, 820), "layer": (1600, 800)}
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


def dock_and_map(d: QDockWidget, map_px: int = 360, top: int = 60) -> QRect:
    """The dock, the map beside it and the toolbars above (from `top`), in the main window's pixels."""
    rect = QRect(d.mapTo(win, d.rect().topLeft()), d.size())
    return QRect(max(0, rect.left() - map_px), top, rect.width() + map_px, rect.bottom() - top + 1)


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


def flow_obs():
    catalog = dock("SatObsImageCatalog")
    obs = dock("SatObsObservations")
    layout(left=[catalog], right=[obs], widths=(430, OBS_WIDTH))
    canvas.setExtent(QgsRectangle(139.70, 35.25, 139.80, 35.35))
    canvas.refresh()
    yield 800
    log(f"obs dock hint {obs.widget().minimumSizeHint()} width {obs.width()}")
    # 1 画像カタログで探す.
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("extent"))
    today = QDate.currentDate()
    catalog.acquired_from.setText(today.addDays(-7).toString("yyyy-MM-dd"))
    catalog.acquired_to.setText(today.toString("yyyy-MM-dd"))
    catalog.search()
    yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
    catalog.table.resizeColumnsToContents()
    quiet_bar()
    under = moved(catalog.buttons["open"].rect(), catalog.buttons["open"], win).bottom() + 5
    shoot(path("obs-01-search"), win, marks=[(toolbar_button("画像カタログ"), "1"), (catalog.area_box, "2"), (catalog.acquired_from, "3"),
                                             (catalog.search_button, "4")], crop=window_part(0, 760, 0, under))
    # 2 開く.
    newest = max(catalog.model.records, key=lambda r: r.attributes["ACQ_TIME"])
    row = catalog.model.records.index(newest)
    catalog.table.selectRow(row)
    yield 300
    shoot(path("obs-02-open"), catalog, marks=[(table_row(catalog.table, row, catalog), "1"), (catalog.buttons["open"], "2")],
          crop=[catalog.table, catalog.buttons["open"]], margin=6)
    catalog.open_records([newest])
    yield lambda: any(isinstance(l, QgsRasterLayer) for l in QgsProject.instance().mapLayers().values())
    yield 1500
    image_id = newest.id
    log(f"opened {image_id}")
    # 3 観測記録ドック: the store, the image, the 分担 line. The map wider, the catalog's work done.
    layout(right=[obs], widths=(OBS_WIDTH,))
    if obs.store_select.findText("モック観測記録") >= 0:
        obs.store_select.setCurrentIndex(obs.store_select.findText("モック観測記録"))
    yield lambda: obs.book is not None and obs.book.loaded and obs.book.current is not None
    yield lambda: obs.assignment_bar.read_at > 0
    log(f"obs content min {obs.scroll.widget().minimumSizeHint()} viewport {obs.scroll.viewport().width()} win {win.size()}")
    ships = next(p for p in sample_images() if p["id"] == image_id)["ships"]
    lons, lats = [s["lon"] for s in ships], [s["lat"] for s in ships]
    ship_area = (min(lons) - 0.004, min(lats) - 0.004, max(lons) + 0.004, max(lats) + 0.004)
    fit_right(canvas, *ship_area, strip=OBS_MAP, border=70)
    yield 1500
    quiet_bar()
    shoot(path("obs-03-image"), win, marks=[(obs.store_select, "1"), (obs.image_select, "2"), (obs.assignment_bar, "3")],
          crop=dock_and_map(obs, OBS_MAP), width=WIDE)
    # 4 クリック記録: the image is taken (分担) first.
    obs.record_button.setChecked(True)
    yield lambda: obs.active
    yield lambda: obs.assignment_bar.read_at > 0 and obs.assignment_bar.done_button.isVisible()
    yield 500
    quiet_bar()
    shoot(path("obs-04-record"), obs, marks=[(obs.record_button, "1"), (obs.assignment_bar.label, "2")],
          crop=[obs.store_select, obs.record_button, obs.status], margin=6)
    # Close to the ship to record.
    new = next(s for s in ships if s["n"] == 2)
    d = 0.007
    canvas.setExtent(QgsRectangle(new["lon"] - d * 1.4, new["lat"] - d, new["lon"] + d * 1.4, new["lat"] + d))
    canvas.refresh()
    yield 1500
    k = 0.0011
    near = map_rect(canvas, new["lon"] - k, new["lat"] - k, new["lon"] + k, new["lat"] + k)
    shoot(path("obs-05-click"), canvas, marks=[(near, "1")], crop=near.adjusted(-260, -150, 260, 150), pad=0)
    obs.map_clicked(new["lon"], new["lat"], None)
    yield lambda: obs.form.isVisible()
    obs.choose_kind(["船舶", "貨物船"])
    obs._pick_activity("停泊")
    obs.confidence_select.setCurrentIndex(max(0, obs.confidence_select.findData("推定")))
    obs.note_input.setText("岸壁の沖で停泊")
    yield 500
    shoot(path("obs-06-form"), obs.form, marks=[(obs.kind_levels, "1"), (obs.activity_input, "2"), (obs.confidence_select, "3"),
                                                (obs.submit_button, "4")])
    obs.submit()
    yield lambda: not obs.form.isVisible()
    yield 1000
    # 5 前の画像にいたモノ (白抜きの印): click it, 継続.
    ghost = next((m for m in obs.scene().marks if m.role == "ghost"), None)
    if ghost is not None:
        canvas.setExtent(QgsRectangle(ghost.lon - d * 1.4, ghost.lat - d, ghost.lon + d * 1.4, ghost.lat + d))
        canvas.refresh()
        yield 1500
        near = map_rect(canvas, ghost.lon - k, ghost.lat - k, ghost.lon + k, ghost.lat + k)
        shoot(path("obs-07-ghost"), canvas, marks=[(near, "1")], crop=near.adjusted(-260, -150, 260, 150), pad=0)
        obs.map_clicked(ghost.lon, ghost.lat, ghost)
        yield lambda: obs.form.isVisible()
        obs._pick_activity("停泊")
        yield 500
        shoot(path("obs-08-carry"), obs.form, marks=[(obs.submit_button, "2"), (obs.absent_button, "3", "top")])
        obs.submit()
        yield lambda: not obs.form.isVisible()
        yield 1000
    obs.record_button.setChecked(False)
    fit_right(canvas, *ship_area, strip=OBS_MAP, border=70)
    yield 2000
    quiet_bar()
    shoot(path("obs-09-saved"), win, marks=[(table_rows(obs.table, win), "1")], crop=dock_and_map(obs, OBS_MAP), width=WIDE)
    # 6 完了.
    shoot(path("obs-10-done-before"), obs, marks=[(obs.assignment_bar.done_button, "1")], crop=[obs.assignment_bar], margin=(4, 5))
    obs.assignment_bar.done_button.click()
    yield lambda: "完了" in obs.assignment_bar.label.text()
    yield 500
    shoot(path("obs-11-done-after"), obs, crop=[obs.assignment_bar], margin=(4, 5))
    layout(left=[catalog], right=[obs], widths=(430, OBS_WIDTH))
    catalog.search()
    yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
    yield 1500
    catalog.table.resizeColumnsToContents()
    yield 300
    shoot(path("obs-12-catalog-done"), catalog, marks=[(table_row(catalog.table, 0, catalog), "")],
          crop=[table_rows(catalog.table, catalog)], margin=4)
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


def flow_request():
    requests = dock("SatObsRequests")
    layout(right=[requests], widths=(620,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: requests.store is not None and requests.task is None and bool(requests.requests)
    yield 1500
    requests.group_filter.choose("*mine*")  # 自分のグループ (core/request/groups.MINE)
    yield 800
    quiet_bar()
    button = toolbar_button("撮像依頼")
    shoot(path("req-01-dock"), win, marks=[(button, "1", "bottom"), (requests.group_filter, "2"), (requests.new_button, "3")],
          crop=[dock_and_map(requests, PLAN_MAP), button], margin=4, width=WIDE)
    requests.edit(new=True)
    form = requests.form
    yield lambda: form is not None and form.isVisible()
    form.resize(600, 1040)
    form.move(win.x() + 150, 5)
    form.name.setText(MINE)
    if form.group.findText("第1班") >= 0:
        form.group.setCurrentIndex(form.group.findText("第1班"))
    form.category.setCurrentText("臨時")
    form.achieve.setCurrentText("期間内1回")
    form.start.setDate(QDate.currentDate())
    form.end.setDate(QDate.currentDate().addDays(7))
    high = form.level.findText("高", Qt.MatchFlag.MatchStartsWith)
    if high >= 0:
        form.level.setCurrentIndex(high)
        form.priority.setValue(form.level.itemData(high))
    form.kind.setCurrentText("光学")
    form.requester.setText("田中（第1班）")
    form.note.setPlainText("岸壁の船の入れ替わりを確認したい")
    w, south, e, n = MINE_AREA
    form._extent_drawn(QgsRectangle(w, south, e, n))
    yield lambda: form.chances_label.isVisible() and form.chances_label.text() != ""
    yield 1500
    buttons = form.findChildren(QDialogButtonBox)[0]
    save = buttons.button(QDialogButtonBox.StandardButton.Save)
    shoot(path("req-02-form"), form, marks=[(form.name, "1"), (form.group, "2"), (form.category, "3"), (form.start, "4"),
                                            (form.level, "5"), (form.kind, "6"), (form.box_button, "7"), (form.chances_label, "8"),
                                            (save, "9")])
    canvas.setExtent(QgsRectangle(139.715, 35.288, 139.787, 35.336))
    canvas.refresh()
    yield 1500
    shoot(path("req-03-area"), canvas, width=560)
    save.click()
    yield lambda: not form.isVisible()
    yield lambda: any(r.name == MINE for r in requests.shown)
    yield 1200
    row = next(i for i, r in enumerate(requests.shown) if r.name == MINE)
    requests.table.selectRow(row)
    hide_columns(requests.table, [requests.model.headerData(c, Qt.Orientation.Horizontal) for c in range(requests.model.columnCount())],
                 ("管理番号", "識別番号"))
    yield 800
    shoot(path("req-04-saved"), requests, marks=[(table_row(requests.table, row, requests), "")],
          crop=[requests.status, table_rows(requests.table, requests)], margin=4)
    log(f"requests shown {len(requests.shown)} mine row {row}")


#: The columns of 「撮像計画ファイル出力」 left out of its pictures, so that 結果・理由 shows in the dock's width.
PLAN_HIDDEN = ("システム", "依頼グループ", "区分", "雲量", "枚数", "繰り上げ")
#: The same for 「撮像予定」.
SCHEDULE_HIDDEN = ("管理番号", "システム", "撮像", "依頼者")


def flow_plan():
    from mikage.ui.plan_file_dock import COLUMNS

    files = dock("SatObsPlanFiles")
    layout(right=[files], widths=(780,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: files.store is not None
    from mikage.core.request.daily import TAKEN

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
    button = toolbar_button("撮像計画ファイル出力")
    shoot(path("plan-01-calc"), win, marks=[(button, "1", "bottom"), (files.day, "2", "top"), (files.run_button, "3", "top"),
                                            (table_rows(files.table, win), "4")],
          crop=[dock_and_map(files, PLAN_MAP), button], margin=4, width=WIDE)
    shoot(path("plan-02-table"), files, crop=[files.status, table_rows(files.table, files)], margin=4)
    files.on_map.setChecked(True)
    files.draw()
    canvas.setExtent(QgsRectangle(139.55, 35.17, 139.95, 35.43))
    canvas.refresh()
    yield 2000
    shoot(path("plan-03-map"), canvas, width=WIDE)
    # 書き出し: the files of each system, after asking (the folder shown as a user's).
    files.folder.setText(SHOWN_FOLDER)
    row = moved(files.folder.rect(), files.folder, files).united(moved(files.write_button.rect(), files.write_button, files))
    shoot(path("plan-04-write"), files, marks=[(files.folder, "1", "bottom"), (files.write_button, "2", "top")],
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
    shoot(path("plan-05-confirm"), box)
    box.button(QMessageBox.StandardButton.Yes).click()
    yield lambda: modal() is None
    yield lambda: files.task is None
    yield 2500
    quiet_bar()
    shoot(path("plan-06-written"), files, marks=[(files.status, "")], crop=[files.status, table_rows(files.table, files)], margin=4)
    log("written: " + ", ".join(sorted(os.listdir(EXPORTS))))
    # 採否: the optical system answers the requester's request in its file; the other one is told by phone (by hand).
    answer = write_answer(times)
    schedule = dock("SatObsSchedule")
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: schedule.store is not None
    schedule.set_day(tomorrow().toPyDate())
    yield lambda: schedule.task is None and schedule.day is not None
    yield 1500
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    button = toolbar_button("撮像予定")
    shoot(path("plan-07-schedule"), win, marks=[(button, "1", "bottom"), (schedule.calendar, "2"), (schedule.import_button, "3")],
          crop=[dock_and_map(schedule, PLAN_MAP), button], margin=4, width=WIDE)
    later(lambda: schedule.import_file(path=answer, ask=True))
    yield lambda: isinstance(modal(), QMessageBox)
    yield 500
    box = modal()
    shoot(path("plan-08-import"), box)
    box.button(QMessageBox.StandardButton.Yes).click()
    yield lambda: modal() is None
    yield lambda: schedule.task is None
    yield 2500
    others = [i for i, r in enumerate(schedule.shown) if r.name == BY_PHONE and r.adoption == "不明"] if schedule.shown else []
    log(f"schedule rows {[(r.name, getattr(r, 'adoption', '')) for r in schedule.shown]}")
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    if others:
        schedule.table.selectRow(others[0])
        schedule.reason.setText("電話で確認（競合）")
        schedule.reason.setCursorPosition(0)
        yield 500
        shoot(path("plan-09-by-hand"), schedule, marks=[(table_row(schedule.table, others[0], schedule), "1"), (schedule.reject_button, "2", "top"),
                                                         (schedule.reason, "3", "top")],
              crop=[schedule.table, schedule.adopt_button, schedule.reason], margin=4)
        schedule.answer("不採用")
        yield lambda: schedule.task is None
        yield 2000
    schedule.table.clearSelection()
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 500
    shoot(path("plan-10-answered"), schedule, crop=[schedule.status, table_rows(schedule.table, schedule)], margin=4)


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


def flow_check():
    schedule = dock("SatObsSchedule")
    places = dock("SatObsPlaceSchedule")
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,))
    yield 500
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield lambda: schedule.store is not None
    schedule.set_day(tomorrow().toPyDate())
    yield lambda: schedule.task is None
    schedule.group_filter.choose("*mine*")  # 自分のグループ (core/request/groups.MINE)
    yield 1000
    mine = [i for i, r in enumerate(schedule.shown) if r.name == MINE]
    if mine:
        schedule.table.selectRow(mine[0])
    hide_columns(schedule.table, schedule_columns(), SCHEDULE_HIDDEN)
    yield 1500
    # Choosing the row moved the map to the request: back into the part of the map shown.
    fit_right(canvas, *MINE_AREA, strip=PLAN_MAP, border=70)
    yield 1000
    quiet_bar()
    button = toolbar_button("撮像予定")
    shoot(path("check-01-schedule"), win, marks=[(button, "1", "bottom"), (schedule.calendar, "2"), (schedule.group_filter, "3", "top")] +
          ([(table_row(schedule.table, mine[0], win), "4")] if mine else []),
          crop=[dock_and_map(schedule, PLAN_MAP), button], margin=4, width=WIDE)
    shoot(path("check-02-row"), schedule, marks=[(table_row(schedule.table, mine[0], schedule), "")] if mine else [],
          crop=[schedule.status, table_rows(schedule.table, schedule)], margin=4)
    # 地点ごとの撮像予定: the request's place on a timeline.
    layout(right=[schedule], widths=(SCHEDULE_WIDTH,), bottom=[places])
    win.resizeDocks([places], [300], Qt.Orientation.Vertical)
    w, south, e, n = MINE_AREA
    places.add_point((w + e) / 2, (south + n) / 2, "岸壁の沖")
    yield lambda: getattr(places, "task", None) is None
    yield 2500
    shoot(path("check-03-places"), places, marks=[(toolbar_button("地点ごとの撮像予定"), ""), (places.pick, "1", "top"), (places.timeline, "2")])
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
    shoot(path("imp-01-layer"), win, marks=[(requests.import_button, "1", "top")],
          crop=dock_and_map(requests, PLAN_MAP, top=requests.mapTo(win, requests.rect().topLeft()).y() - 4), width=WIDE)
    dialog = requests.import_layer(layer)
    yield lambda: dialog.isVisible()
    dialog.point_km.setValue(1.0)
    yield 500
    shoot(path("imp-02-choose"), dialog, marks=[(dialog.layer, "1"), (dialog.name_field, "2"), (dialog.management_field, "3"),
                                                (dialog.simple, "4"), (dialog.ok, "5")])
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
    save = form.findChildren(QDialogButtonBox)[0].button(QDialogButtonBox.StandardButton.Save)
    shoot(path("imp-03-form"), form, marks=[(form.name, "1"), (form.area_label, "2"), (form.category, "3"), (form.start, "4"),
                                            (form.level, "5"), (save, "6")])
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
    shoot(path("imp-04-saved"), requests, marks=[(table_row(requests.table, i, requests), "") for i in rows],
          crop=[requests.status, table_rows(requests.table, requests)], margin=4)
    log(f"imported: {requests.status.text()}")


def flow_sample():
    """The first two big steps of the observation flow, in finer steps (a sample for the proposal)."""
    catalog = dock("SatObsImageCatalog")
    obs = dock("SatObsObservations")
    layout(right=[obs], widths=(OBS_WIDTH,))
    catalog.hide()
    canvas.setExtent(QgsRectangle(139.70, 35.25, 139.80, 35.35))
    canvas.refresh()
    yield 800
    quiet_bar()
    # 始める前に: the buttons of this flow, each as it is on the toolbar (shown in the text).
    cat_button, obs_button = toolbar_button("画像カタログ"), toolbar_button("観測記録")
    save_button(path("s-btn-catalog"), cat_button)
    save_button(path("s-btn-obs"), obs_button)
    # 1-1 the dock.
    layout(left=[catalog], right=[obs], widths=(430, OBS_WIDTH))
    obs.hide()
    yield 800
    quiet_bar()
    top = moved(catalog.rect(), catalog, win)
    shoot(path("s-obs-01-dock"), win, marks=[(cat_button, "1", "bottom"), (QRect(top.left(), top.top(), top.width(), 22), "2", "right")],
          crop=window_part(0, 760, 0, top.top() + 300))
    # 1-2 範囲: the list open.
    catalog.area_box.setCurrentIndex(catalog.area_box.findData("extent"))
    catalog.area_box.showPopup()
    yield 600
    popup = catalog.area_box.view().window()
    form_top = global_rect(catalog.catalog_box)
    area = global_rect(catalog).adjusted(0, 0, 0, 0)
    area = QRect(area.left(), form_top.top() - 8, area.width(), max(global_rect(popup).bottom(), global_rect(catalog.acquired_from).bottom()) - form_top.top() + 16)
    shoot_screen(path("s-obs-02-area"), area, marks=[(catalog.area_box, "1"), (global_rect(popup), "")])
    catalog.area_box.hidePopup()
    yield 300
    # 1-3 撮像日時.
    today = QDate.currentDate()
    catalog.acquired_from.setText(today.addDays(-7).toString("yyyy-MM-dd"))
    catalog.acquired_to.setText(today.toString("yyyy-MM-dd"))
    yield 300
    shoot(path("s-obs-03-date"), catalog, marks=[(catalog.acquired_from, "1"), (catalog.acquired_to, "2", "top")],
          crop=QRect(0, catalog.area_box.mapTo(catalog, catalog.area_box.rect().topLeft()).y() - 6, catalog.width(),
                     catalog.keyword.mapTo(catalog, catalog.keyword.rect().bottomLeft()).y() - catalog.area_box.mapTo(catalog, catalog.area_box.rect().topLeft()).y() + 12))
    # 1-4 検索.
    catalog.search()
    yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
    catalog.table.resizeColumnsToContents()
    yield 500
    quiet_bar()
    shoot(path("s-obs-04-search"), catalog, marks=[(catalog.search_button, "1"), (catalog.status, "2", "top"), (table_rows(catalog.table, catalog), "3")],
          crop=[catalog.search_button, table_rows(catalog.table, catalog)], margin=6)
    # 2-1 選ぶ: the row and its footprint on the map.
    newest = max(catalog.model.records, key=lambda r: r.attributes["ACQ_TIME"])
    row = catalog.model.records.index(newest)
    from mikage.qgis_io.catalog_io import footprint
    shape = footprint(newest.geometry)
    ext = shape.boundingBox() if shape is not None else None
    if ext is not None:
        fit_left(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum(), strip=440, border=40)
    catalog.table.selectRow(row)
    yield 1500
    quiet_bar()
    left = moved(catalog.rect(), catalog, win)
    marks = [(table_row(catalog.table, row, win), "1")]
    if ext is not None:
        marks.append((map_rect(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()).translated(canvas.mapTo(win, canvas.rect().topLeft())).adjusted(-6, -6, 6, 6), "2", "top"))
    shoot(path("s-obs-05-select"), win, marks=marks, crop=window_part(0, left.right() + 440, left.top(), left.bottom()), width=WIDE)
    # 2-2 開く.
    shoot(path("s-obs-06-open"), catalog, marks=[(catalog.buttons["open"], "1"), (catalog.close_previous, ""), (catalog.zoom_opened, "")],
          crop=[catalog.buttons["open"], catalog.zoom_opened], margin=6)
    catalog.open_records([newest])
    yield lambda: any(isinstance(l, QgsRasterLayer) for l in QgsProject.instance().mapLayers().values())
    yield 2500
    if ext is not None:
        fit_left(canvas, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum(), strip=440, border=40)
    yield 2000
    quiet_bar()
    shoot(path("s-obs-07-opened"), win, crop=window_part(0, left.right() + 440, left.top(), left.bottom()), width=WIDE)
    log(f"sample: newest {newest.id} geometry {getattr(newest, 'geometry', None)}")


SCENARIOS = {"sample": flow_sample, "obs": flow_obs, "request": flow_request, "plan": flow_plan, "check": flow_check, "layer": flow_layer}


def set_role(name: str) -> None:
    """The role (担当) ticked alone, as in 「担当と個人設定」: its menus and toolbar buttons (and the mock's table)."""
    from mikage.core import roles

    plugin().role_keeper.choose(roles.codes_of([name]))
    settle(5)


ROLES = {"sample": "観測記録の担当", "obs": "観測記録の担当", "request": "撮像を依頼する人", "plan": "撮像計画の担当", "check": "撮像を依頼する人", "layer": "撮像を依頼する人"}


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
