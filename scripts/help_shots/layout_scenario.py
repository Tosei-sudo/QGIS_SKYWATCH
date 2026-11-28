"""
Every dock (each tab, at a usual and a narrow width) and every dialog of the
plugin's menu, shown in a real QGIS against scripts/mock_esri and checked by
mikage/tests/layout_probe.py for what is drawn over something else or cut
off. Started by check_layout.py (`qgis --code`). Writes one picture per
state and problems.json to SHOTS.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QDialog, QDockWidget, QMessageBox, QTabWidget, QWidget
from qgis.utils import iface, plugins

import shots_lib
from shots_lib import Runner, later, settle

OUT = os.environ["SHOTS"]
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "mikage", "tests"))
import layout_probe  # noqa: E402

shots_lib.TIMEOUT = 15
LOG = open(os.path.join(OUT, "scenario.log"), "w", encoding="utf-8")
#: Dock widths: as the plugin usually opens them, and narrow.
WIDTHS = [int(w) for w in os.environ.get("WIDTHS", "440,300").split(",")]
ONLY = [s for s in os.environ.get("ONLY", "").split(",") if s]
PARTS = [s for s in os.environ.get("PARTS", "docks,dialogs,forms,feed,filled").split(",") if s]
FOUND: dict = {}

win = iface.mainWindow()


def log(text: str) -> None:
    print(text, file=LOG, flush=True)


def plugin():
    return plugins["mikage"]


def record(key: str, root: QWidget, picture: QWidget | None = None) -> None:
    """The picture of `picture` (default: `root`) and the problems under `root`."""
    settle(5)
    found = layout_probe.problems(root)
    FOUND[key] = {"size": [root.width(), root.height()], "problems": found, "squeezed": layout_probe.squeezed(root)}
    target = picture or root
    target.grab().save(os.path.join(OUT, key + ".png"))
    if found:
        log(f"{key}: {len(found)}\n" + layout_probe.summary(found))
    else:
        log(f"{key}: ok")


def tab_states(root: QWidget):
    """Each tab of each tab widget under root shown in turn (the others as they are); yields a suffix per state."""
    yield ""
    for tabs in root.findChildren(QTabWidget):
        if tabs.count() < 2:
            continue
        before = tabs.currentIndex()
        for i in range(tabs.count()):
            if i == before or not tabs.isTabEnabled(i) or (hasattr(tabs, "isTabVisible") and not tabs.isTabVisible(i)):
                continue
            tabs.setCurrentIndex(i)
            settle(5)
            if tabs.isVisible():
                yield f"-{tabs.objectName() or 'tabs'}{i}"
        tabs.setCurrentIndex(before)


def show_alone(d: QDockWidget, width: int) -> None:
    for other in win.findChildren(QDockWidget):
        if other.isVisible() and other is not d:
            other.hide()
    area = Qt.DockWidgetArea.BottomDockWidgetArea if d.objectName() == "SatObsPlaceSchedule" else Qt.DockWidgetArea.RightDockWidgetArea
    if not (d.isVisible() and not d.isFloating() and win.dockWidgetArea(d) == area):
        # Docked again only when it is not yet (a dock hidden on the way closes its form, stops recording…).
        d.setFloating(False)
        win.addDockWidget(area, d)
        d.show()
        d.raise_()
    if area == Qt.DockWidgetArea.RightDockWidgetArea:
        win.resizeDocks([d], [width], Qt.Orientation.Horizontal)
    else:
        win.resizeDocks([d], [320], Qt.Orientation.Vertical)
    settle(10)


def docks():
    names = [n for n in plugin().docks if not ONLY or n in ONLY]
    for name in names:
        d = plugin().docks[name]
        for width in WIDTHS:
            show_alone(d, width)
            yield 2500 if width == WIDTHS[0] else 600
            body = d.scroll.widget() if hasattr(d, "scroll") else d.widget()
            log(f"{name} width {d.width()} body min {body.minimumSizeHint().width()}×{body.minimumSizeHint().height()}")
            for suffix in tab_states(d):
                yield 300
                record(f"dock-{name}-{width}{suffix}", d, body)
        d.hide()


#: Menu entries that open the plugin's own windows (not Processing's).
MENU_DIALOGS = ["年間撮像上限…", "移動目標撮像計画…", "通報文の作成…", "通報記録の確認…", "観測記録の書き出し…", "観測記録の一括取り込み…",
                "分担…", "引き継ぎメモ…", "新規スクラッチレイヤー（テンプレート）…", "検出結果を DB へ転記…", "ArcGIS Web マップを開く…",
                "ArcGIS Web マップとして保存…", "担当と個人設定…", "ツールバーのボタン…", "接続と表の点検…", "監査ログ…", "機能の使われ方…", "使い方（ヘルプ）…",
                "Python API リファレンス…", "設定…"]


def action_of(text: str):
    for a in plugin().actions:
        if a.text() == text:
            return a
    return None


def windows() -> set:
    return {w for w in QApplication.topLevelWidgets() if w.isVisible() and w is not win}


def check_window(key: str, opened):
    """The window that `opened` shows: as it opens, each tab, and at its smallest size."""
    before = windows()
    later(opened, 0)
    yield lambda: windows() - before
    yield 1500
    new = [w for w in windows() - before]
    if not new:
        log(f"{key}: no window")
        return
    w = new[0]
    log(f"{key}: {type(w).__name__} {w.width()}×{w.height()} min {w.minimumSizeHint().width()}×{w.minimumSizeHint().height()}")
    for suffix in tab_states(w):
        yield 300
        record(f"{key}{suffix}", w)
    w.resize(w.minimumSize().expandedTo(w.minimumSizeHint()))
    yield 500
    record(f"{key}-small", w)
    for extra in [x for x in windows() - before if x is not w]:
        extra.close()
    if isinstance(w, QMessageBox):
        w.reject()
    elif isinstance(w, QDialog):
        w.reject()
    else:
        w.close()
    yield 500


def dialogs():
    for i, text in enumerate(MENU_DIALOGS):
        if ONLY and text not in ONLY:
            continue
        action = action_of(text)
        if action is None:
            log(f"no action {text}")
            continue
        yield from check_window(f"dialog-{i:02d}", action.trigger)


def forms():
    """Windows opened from the docks."""
    p = plugin()
    requests = p.docks["SatObsRequests"]
    show_alone(requests, WIDTHS[0])
    yield lambda: requests.store is not None and requests.task is None
    yield from check_window("form-request-new", lambda: requests.edit(new=True))
    events = p.docks.get("SatObsEvents")
    if events is not None:
        show_alone(events, WIDTHS[0])
        yield 1500
        yield from check_window("form-event-new", events.create)


def feed():
    p = plugin()
    for d in win.findChildren(QDockWidget):
        if d.isVisible():
            d.hide()
    p.toggle_notice_feed()
    for size in ((1600, 1000), (1100, 800)):
        win.resize(*size)
        settle(10)
        yield 4000
        page = win.centralWidget()
        record(f"feed-{size[0]}", page)
    p.toggle_notice_feed()
    yield 500


def at_widths(key: str, d: QDockWidget, widths=None):
    """The dock alone at each width: its picture and problems (each tab)."""
    for width in widths or WIDTHS:
        show_alone(d, width)
        yield 800
        body = d.scroll.widget() if hasattr(d, "scroll") else d.widget()
        for suffix in tab_states(d):
            yield 300
            record(f"filled-{key}-{width}{suffix}", d, body)


def guarded(steps):
    """A part of `filled` whose mistake is logged instead of ending the run."""
    import traceback
    try:
        yield from steps
    except Exception:
        log(traceback.format_exc())


def filled():
    """The main docks with something in them (a search done, an image open, a plan calculated…)."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle
    from qgis.PyQt.QtCore import QDate

    p = plugin()
    canvas = iface.mapCanvas()
    canvas.setExtent(QgsRectangle(139.55, 35.17, 139.95, 35.43))
    canvas.refresh()
    catalog, obs = p.docks["SatObsImageCatalog"], p.docks["SatObsObservations"]
    state = {}

    def catalog_steps():
        show_alone(catalog, WIDTHS[0])
        catalog.area_box.setCurrentIndex(catalog.area_box.findData("extent"))
        today = QDate.currentDate()
        catalog.acquired_from.setText(today.addDays(-30).toString("yyyy-MM-dd"))
        catalog.acquired_to.setText(today.toString("yyyy-MM-dd"))
        catalog.search()
        yield lambda: catalog.search_button.isEnabled() and catalog.model.rowCount() > 0
        catalog.table.selectRow(0)
        yield from at_widths("catalog", catalog)
        state["records"] = list(catalog.model.records)

    def obs_steps():
        records = state.get("records") or []
        if not records:
            return
        newest = max(records, key=lambda r: str(r.acquired))
        catalog.open_records([newest])
        yield lambda: any(isinstance(l, QgsRasterLayer) for l in QgsProject.instance().mapLayers().values())
        show_alone(obs, WIDTHS[0])
        if obs.store_select.findText("モック観測記録") >= 0:
            obs.store_select.setCurrentIndex(obs.store_select.findText("モック観測記録"))
        yield lambda: obs.book is not None and obs.book.loaded and obs.book.current is not None
        yield 1500
        yield from at_widths("obs", obs, [660] + WIDTHS)
        show_alone(obs, WIDTHS[0])
        obs.record_button.setChecked(True)
        yield lambda: obs.active
        yield 1000
        c = canvas.extent().center()
        obs.map_clicked(c.x(), c.y(), None)
        yield lambda: obs.form.isVisible()
        yield 500
        yield from at_widths("obs-form", obs, [660] + WIDTHS)
        obs.close_form()
        obs.record_button.setChecked(False)
        yield 500

    def plan_steps():
        plan = p.docks["SatObsImagingPlan"]
        show_alone(plan, WIDTHS[0])
        plan.tree.check_all(True)
        plan.run()
        yield lambda: plan.table.model() is not None and plan.table.model().rowCount() > 0
        yield 2000
        yield from at_widths("plan", plan)

    def pass_steps():
        passes = p.docks["SatObsSatellitePass"]
        show_alone(passes, WIDTHS[0])
        yield 1500
        passes.tree.check_all(True)
        yield 3000
        yield from at_widths("pass", passes)

    def files_steps():
        files = p.docks["SatObsPlanFiles"]
        show_alone(files, WIDTHS[0])
        yield lambda: files.store is not None
        files.day.setDate(QDate.currentDate().addDays(1))
        files.run()
        yield lambda: files.task is None and files.table.model().rowCount() > 0
        yield 1500
        yield from at_widths("files", files)
        files.write(ask=False)
        yield lambda: files.task is None
        yield 1500
        schedule = p.docks["SatObsSchedule"]
        show_alone(schedule, WIDTHS[0])
        yield lambda: schedule.store is not None
        schedule.set_day(QDate.currentDate().addDays(1).toPyDate())
        yield lambda: schedule.task is None and schedule.day is not None
        yield 1500
        if schedule.table.model().rowCount():
            schedule.table.selectRow(0)
        yield from at_widths("schedule", schedule)

    def requests_steps():
        requests = p.docks["SatObsRequests"]
        show_alone(requests, WIDTHS[0])
        yield lambda: requests.store is not None and requests.task is None and bool(requests.requests)
        requests.table.selectRow(0)
        yield from at_widths("requests", requests)

    def places_steps():
        places = p.docks["SatObsPlaceSchedule"]
        show_alone(places, WIDTHS[0])
        places.add_point(139.75, 35.31, "岸壁の沖")
        places.add_point(139.83, 35.38, "木更津沖")
        yield lambda: getattr(places, "task", None) is None
        yield 4000
        for size in ((1600, 1000), (1100, 800)):
            win.resize(*size)
            settle(10)
            yield 800
            record(f"filled-places-{size[0]}", places)
        win.resize(1600, 1000)

    def events_steps():
        events = p.docks["SatObsEvents"]
        show_alone(events, WIDTHS[0])
        yield 1500
        if events.table.rowCount():
            events.table.selectRow(0)
        yield 1500
        yield from at_widths("events", events)

    def taxonomy_steps():
        taxonomy = p.docks["SatObsTaxonomy"]
        show_alone(taxonomy, WIDTHS[0])
        yield 1500
        taxonomy.select(["船舶", "貨物船"])
        yield 800
        yield from at_widths("taxonomy", taxonomy)

    def dashboard_steps():
        dashboard = p.docks["SatObsDashboard"]
        layer = next((l for l in QgsProject.instance().mapLayers().values() if "観測" in l.name() and l.type() == 0), None)
        if layer is not None:
            iface.setActiveLayer(layer)
        show_alone(dashboard, WIDTHS[0])
        yield 2000
        yield from at_widths("dashboard", dashboard)

    def ai_steps():
        ai = p.docks["SatObsAiResults"]
        show_alone(ai, WIDTHS[0])
        yield 3000
        yield from at_widths("ai", ai)

    def feed_steps():
        from mikage.core.catalog.watch import WatchNotice
        import time as _time

        records = state.get("records") or []
        notices = p.docks["SatObsNotices"]
        store = notices.store()
        now = _time.time() * 1000
        label = catalog.catalog_box.currentText()
        store.add_notices([WatchNotice(id=f"w|{r.id}", watch_id="w", watch_name="横須賀の新着", catalog_label=label, found=now - i * 60000,
                                       read=i > 2, image_id=r.id, acquired=r.acquired, sensor=r.sensor, feature=r.feature())
                           for i, r in enumerate(records[:6])])
        notices.refresh()
        yield 1000
        yield from at_widths("notices", notices)
        for d in win.findChildren(QDockWidget):
            if d.isVisible():
                d.hide()
        p.toggle_notice_feed()
        for size in ((1600, 1000), (1100, 800)):
            win.resize(*size)
            settle(10)
            yield 5000
            record(f"filled-feed-{size[0]}", win.centralWidget())
        p.toggle_notice_feed()
        win.resize(1600, 1000)

    for steps in (catalog_steps, obs_steps, requests_steps, plan_steps, pass_steps, files_steps, places_steps, events_steps,
                  taxonomy_steps, dashboard_steps, ai_steps, feed_steps):
        if ONLY and steps.__name__.split("_")[0] not in ONLY:
            continue
        log(f"- {steps.__name__}")
        yield from guarded(steps())


def everything():
    win.move(0, 0)
    win.resize(1600, 1000)
    settle(10)
    from mikage.core import roles

    p = plugin()
    p.role_keeper.choose(roles.codes_of([r.name for r in roles.ROLES]))
    settle(5)
    for part in PARTS:
        log(f"--- {part}")
        yield from {"docks": docks, "dialogs": dialogs, "forms": forms, "feed": feed, "filled": filled}[part]()


def finished():
    with open(os.path.join(OUT, "problems.json"), "w", encoding="utf-8") as f:
        json.dump(FOUND, f, ensure_ascii=False, indent=1)
    log("done")
    LOG.close()
    later(lambda: os._exit(0), 300)


RUNNER = Runner(everything(), log, finished)
later(RUNNER.start, 6000)
