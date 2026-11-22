"""
チュートリアル's courses (ui/tutorial_guide.py runs them): 「はじめての
MIKAGE」 and the flows of the help's 画面つきの手順書 (040-howto.html), with
its numbers and words — 観測記録 (1-1 … 9-3), 撮像依頼 (sections 1, 2, 3, 5, 6,
7), レイヤーから取り込み (its section 4) and 撮像計画 (1-1 … 5-1). Each step
says what to do, frames where, knows when it is done from the screen, and
can be shown (「やって見せる」: what the help's pictures were taken with,
scripts/help_shots/scenario.py).

A course is made on the practice data (練習モード), with the docks reached
through the plugin's API and `place` (features/training.py's TutorialPlace):
`api`, `canvas`, `button(entry)`, `trigger(entry)`, `help_window()`, `band`,
`toolbar`, `ships(image id)`, `outputs`, `builtin` and `to_sample()`. The
classes may be the organisation's own (練習モード reads them from the
production): a step choosing a kind takes the first one offered when the
sample's is not there.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Callable, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject
from qgis.core import QgsRasterLayer, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import QDate, QPoint, QRect, Qt, QTimer, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication, QDialogButtonBox, QMainWindow, QMessageBox

from ..core.request.api import answer_bytes, AWAITING, DECLINED, handed_out, HELD_BACK, MANUAL, MINE, PLANNED
from .tutorial_guide import Counter, Course, Step

#: The request made in 撮像依頼 (as in the help's pictures), and its area (west, south, east, north).
REQUEST_NAME = "練習 横須賀沖 岸壁"
REQUEST_AREA = (139.738, 35.302, 139.764, 35.322)
#: The places of the layer of レイヤーから取り込み: name, 管理番号, outline (lon, lat).
PLACES = [
    ("北の岸壁", "K-101", [(139.736, 35.331), (139.757, 35.329), (139.760, 35.341), (139.744, 35.346), (139.734, 35.340)]),
    ("中の岸壁", "K-102", [(139.768, 35.300), (139.790, 35.298), (139.792, 35.314), (139.771, 35.316)]),
    ("南の錨地", "K-103", [(139.735, 35.262), (139.761, 35.258), (139.766, 35.276), (139.748, 35.284), (139.733, 35.276)]),
]
PLACES_LAYER = "見たい岸壁（練習）"
BASEMAP_NAME = "練習用の背景地図"
#: Where 「返ってきた採否を入れる」's answer files are made (in the practice's output folder).
ANSWERS_FOLDER = "返ってきた採否"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


# ---- the screen ------------------------------------------------------------------------------------------

def shown(widget) -> bool:
    """Whether a dock (or any widget) is on the screen (a dock behind another's tab is not)."""
    try:
        return widget is not None and widget.isVisible() and not widget.visibleRegion().isEmpty()
    except RuntimeError:
        return False


#: A dock smaller than this (pixels) is made bigger when a course brings it out: what the steps point at must show.
DOCK_SIZE = (340, 380)


def show(dock) -> None:
    """A dock out and in front (behind another's tab: brought forward), big enough to see what the steps point at."""
    dock.show()
    dock.raise_()
    main = dock.parentWidget()
    if isinstance(main, QMainWindow) and not dock.isFloating():
        if dock.width() < DOCK_SIZE[0]:
            main.resizeDocks([dock], [max(DOCK_SIZE[0], 420)], Qt.Orientation.Horizontal)
        if dock.height() < DOCK_SIZE[1]:
            main.resizeDocks([dock], [max(DOCK_SIZE[1], min(620, main.height() * 2 // 3))], Qt.Orientation.Vertical)


def reveal(dock, name: str) -> tuple:
    """The button shown while a dock is not on the screen."""
    return (f"{name}のドックを出す", lambda: show(dock))


def _transform(canvas) -> QgsCoordinateTransform:
    return QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())


def on_map(canvas, lon: float, lat: float, half: int = 16) -> Optional[QRect]:
    """A box round a place (longitude, latitude) on the screen; None when the map does not show it."""
    if not shown(canvas):
        return None
    point = _transform(canvas).transform(QgsPointXY(lon, lat))
    pixel = canvas.getCoordinateTransform().transform(point)
    x, y = int(pixel.x()), int(pixel.y())
    if not (0 <= x < canvas.width() and 0 <= y < canvas.height()):
        return None
    corner = canvas.mapToGlobal(QPoint(x - half, y - half))
    return QRect(corner.x(), corner.y(), 2 * half, 2 * half)


def fit(canvas, west: float, south: float, east: float, north: float) -> None:
    """The map moved to show a box (longitude / latitude)."""
    box = _transform(canvas).transformBoundingBox(QgsRectangle(west, south, east, north))
    canvas.setExtent(box)
    canvas.refresh()


def save_button(form):
    boxes = form.findChildren(QDialogButtonBox)
    return boxes[0].button(QDialogButtonBox.StandardButton.Save) if boxes else None


def question_yes():
    """The はい of the question open now (a QMessageBox), None when none."""
    box = QApplication.activeModalWidget()
    return box.button(QMessageBox.StandardButton.Yes) if isinstance(box, QMessageBox) else None


def raster_ids() -> set:
    return {i for i, layer in QgsProject.instance().mapLayers().items() if isinstance(layer, QgsRasterLayer)
            and layer.name() != BASEMAP_NAME}


def S(number: str, title: str, text: Any, **kw) -> Step:  # noqa: N802 (short: the courses are lists of them)
    return Step(number, title, text, **kw)


# ---- はじめての MIKAGE -----------------------------------------------------------------------------------

def first_course(p) -> Course:
    api, canvas = p.api, p.canvas
    catalog = api.image_catalog
    jump = getattr(api.coordinates, "field", None)

    def moved(before: Optional[QgsRectangle]) -> bool:
        now = canvas.extent()
        if before is None or before.width() <= 0:
            return False
        shift = abs(now.center().x() - before.center().x()) + abs(now.center().y() - before.center().y())
        return shift > before.width() * 0.05 or abs(now.width() / before.width() - 1) > 0.1

    return Course("first", "はじめての MIKAGE", "", "練習中の印、ボタンとドック、地図の動かし方、ヘルプ、練習の終え方。どの担当の人も最初に。",
                  "training", [
        S("1", "練習中の印を見る",
          "地図の上の黄色い帯、ステータスバーの「練習モード」、ドックの名前の【練習】が、練習中の印です。"
          "ここで記録・依頼したものは練習用のデータに入り、本番のサーバーには書き込みません。",
          target=lambda: p.band),
        S("2", "ツールバーのボタン",
          "MIKAGE の操作は、ツールバーのボタン（赤い枠）か、メニューの <b>MIKAGE</b> から始めます。"
          "ボタンにマウスを重ねると、名前と説明が出ます。",
          target=lambda: p.toolbar),
        S("3", "担当を選ぶ",
          "メニューの <b>MIKAGE ＞ 担当と個人設定…</b> で自分の担当（観測記録の担当・撮像を依頼する人など）を選ぶと、"
          "担当に合ったボタンとドックが並びます。練習中に変えた担当は、練習を終えると元に戻ります。"
          "チュートリアルのコースを始めると、そのコースの担当が足されます。",
          target=lambda: p.button("担当と個人設定…") or p.toolbar),
        S("4", "地図を動かす",
          "<b>座標・地名へ移動</b> の欄（赤い枠）に「35.31 139.75」のように緯度・経度を入れて Enter を押すと、その場所へ移動します。"
          "マウスのホイールで拡大・縮小、ドラッグで移動もできます。どれかで地図を動かすと、次に進みます。",
          target=lambda: jump if shown(jump) else canvas,
          begin=lambda: QgsRectangle(canvas.extent()), done=moved,
          demo=lambda: api.coordinates.jump("35.31 139.75"), ok="地図がその場所へ移ります"),
        S("5", "ドックを出す",
          "ツールバーの <b>画像カタログ</b> を押します。画面の左に、画像カタログのドックが開きます。"
          "MIKAGE の機能の多くは、このようなドック（地図の横の欄）で使います。",
          target=lambda: catalog if shown(catalog) else p.button("画像カタログ"),
          done=lambda _b: shown(catalog), demo=lambda: show(catalog), ok="画像カタログのドックが開きます"),
        S("6", "ドックをしまう",
          "ドックの右上の × を押すと閉じます。ツールバーのボタンをもう一度押しても出し入れできます。"
          "ドックの名前の所をドラッグすると、置き場所を変えたり、別の窓にしたりできます。",
          target=lambda: catalog if shown(catalog) else None,
          done=lambda _b: not catalog.isVisible(), demo=catalog.hide),
        S("7", "ヘルプを開く",
          "<b>使い方（ヘルプ）…</b> を押すと、ヘルプが開きます（メニューの MIKAGE からも）。"
          "「画面つきの手順書」には、このチュートリアルと同じ操作が写真つきで載っています。",
          target=lambda: p.button("使い方（ヘルプ）…") or p.toolbar,
          done=lambda _b: shown(p.help_window()), demo=lambda: p.trigger("使い方（ヘルプ）…"), ok="ヘルプが開きます"),
        S("8", "練習を終える・最初に戻す",
          "練習を終えるときは、地図の上の <b>練習モードを終える</b> を押します（QGIS を閉じても本番に戻ります）。"
          "<b>練習データを最初に戻す…</b> で、見本を最初の状態に戻せます。ほかのコースは <b>チュートリアル</b> から選びます。",
          target=lambda: list(p.band.buttons.values()) if p.band is not None else None),
    ])


# ---- 画像を探して記録し、担当を完了する（観測記録の担当） -------------------------------------------

def obs_course(p) -> Course:
    api, canvas = p.api, p.canvas
    catalog, obs = api.image_catalog, api.observation_dock
    today = date.today()
    first_day, last_day = (today - timedelta(days=7)).isoformat(), today.isoformat()

    def ghosts() -> list:
        return [m for m in obs.scene().marks if m.role == "ghost"]

    def on_ghost() -> bool:
        return obs.form.isVisible() and obs.pending is not None and obs.pending[3] is not None

    def new_ship() -> Optional[tuple]:
        """A thing of the image not recorded yet (the sample's ship), else the image's middle."""
        book = obs.book
        if book is None or book.current is None:
            return None
        marks = [m for m in obs.scene().marks if m.role in ("obs", "ghost")]

        def recorded(s: dict) -> bool:
            return any(abs(m.lon - s["lon"]) < 0.0012 and abs(m.lat - s["lat"]) < 0.0009 for m in marks)

        free = [s for s in p.ships(book.current.id) if not recorded(s)]
        if free:
            return free[0]["lon"], free[0]["lat"]
        box = book.current.box
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) if box else None

    def ship_rect():
        at = new_ship()
        return on_map(canvas, *at, half=22) if at is not None else None

    def click_ship() -> None:
        if not obs.active:
            obs.record_button.setChecked(True)
        at = new_ship()
        if at is None:
            raise ValueError("観測記録のドックで画像が選ばれていません")
        lon, lat = at
        d = 0.006
        fit(canvas, lon - d * 1.4, lat - d, lon + d * 1.4, lat + d)
        # (two points or more per thing in this organisation: its rear, front, sides)
        for dx, dy in [(0, 0), (0.0004, 0), (0.0002, 0.00008), (0.0002, -0.00008)][:max(1, obs.points)]:
            obs.map_clicked(lon + dx, lat + dy, None)

    def form_new() -> bool:
        return obs.form.isVisible() and obs.pending is not None and obs.pending[2] is None and obs.pending[3] is None

    def choose_kind() -> None:
        rows = obs.kind_level_rows()
        if not rows or not rows[0][1]:
            raise ValueError("分類がありません")
        path = ["船舶" if "船舶" in rows[0][1] else rows[0][1][0]]
        obs.choose_kind(path)
        rows = obs.kind_level_rows()
        if len(rows) > 1 and rows[1][1]:
            path.append("貨物船" if "貨物船" in rows[1][1] else rows[1][1][0])
            obs.choose_kind(path)

    def choose_activity() -> None:
        words = obs.activity_words()
        obs.pick_activity("停泊" if "停泊" in words or not words else words[0])
        guess = obs.confidence_select.findData("推定")
        if guess >= 0:
            obs.confidence_select.setCurrentIndex(guess)

    def submit_new() -> None:
        if not obs.note_input.text().strip():
            obs.note_input.setText("練習: 岸壁の沖で停泊")
        obs.submit()

    def ghost_rect():
        rects = [r for r in (on_map(canvas, m.lon, m.lat, 18) for m in ghosts()) if r is not None]
        return rects[0] if rects else None

    def go_to_ghost() -> None:
        marks = ghosts()
        if marks:
            m, d = marks[0], 0.006
            fit(canvas, m.lon - d * 1.4, m.lat - d, m.lon + d * 1.4, m.lat + d)

    def click_ghost() -> None:
        if not obs.active:
            obs.record_button.setChecked(True)
        marks = ghosts()
        if not marks:
            raise ValueError("前の画像のモノ（白抜きの印）がありません")
        go_to_ghost()
        obs.map_clicked(marks[0].lon, marks[0].lat, marks[0])

    def carry() -> None:
        if not on_ghost():
            click_ghost()
        obs.submit()

    def open_first() -> None:
        records = catalog.chosen() or list(catalog.model.records[:1])
        if not records:
            raise ValueError("一覧に画像がありません（1. で検索します）")
        catalog.open_records(records[:1])

    def done_assignment() -> None:
        button = obs.assignment_bar.done_button
        if not button.isVisible():
            raise ValueError("この画像はまだ自分の担当ではありません（4. のクリック記録で自分の担当になります）")
        button.click()

    def searched(c) -> bool:
        return c is not None and c.count > 0 and catalog.model.rowCount() > 0 and catalog.search_button.isEnabled()

    def setup() -> str:
        if p.builtin:
            p.to_sample()
            return "地図を練習用の見本の場所（横須賀）に動かしました。"
        return ""

    no_ghost = "この画像には、前の画像のモノ（白抜きの印）が残っていません。"
    form_reveal = ("記録のフォームを開き直す", click_ship)
    return Course("obs", "画像を探して記録し、担当を完了する", "OBSERVER",
                  "画像カタログで検索 → 画像を開く → クリック記録 → 前の画像のモノを継続・消失 → 分担を完了（19 の手順）。",
                  "howto-obs", [
        S("1-1", "画像カタログのドックを出す",
          "ツールバーの <b>画像カタログ</b>（赤い枠）を押します。画面の左に、画像カタログのドックが開きます。",
          target=lambda: catalog if shown(catalog) else p.button("画像カタログ"),
          done=lambda _b: shown(catalog), demo=lambda: show(catalog), ok="画像カタログのドックが開きます"),
        S("1-2", "探す範囲を選ぶ",
          "<b>範囲</b> の ▼ を押して、どこに写っている画像を探すかを選びます。ふだんは <b>表示範囲</b> のままにして、"
          "先に地図を見たい場所へ動かしておきます（練習では、地図はもう見本の場所にあります）。"
          "見たい場所が地図の表示範囲と合わないときは、横の <b>範囲を描く</b> で地図に多角形を描きます。",
          target=lambda: [catalog.area_box, catalog.draw_button], reveal=reveal(catalog, "画像カタログ"),
          demo=lambda: (catalog.area_box.setCurrentIndex(max(0, catalog.area_box.findData("extent"))), p.builtin and p.to_sample())),
        S("1-3", "撮像日時を入れる",
          f"<b>撮像日時</b> の左に最初の日、右に最後の日を入れます（例: {first_day} と {last_day}。練習の見本は、この 1 週間に撮った画像です）。"
          "日付だけなら、その日いっぱい（0:00〜23:59）が入ります。",
          target=lambda: [catalog.acquired_from, catalog.acquired_to], reveal=reveal(catalog, "画像カタログ"),
          done=lambda _b: bool(catalog.acquired_from.text().strip()) and bool(catalog.acquired_to.text().strip())
          and catalog.input_problem() is None,
          demo=lambda: (catalog.acquired_from.setText(first_day), catalog.acquired_to.setText(last_day))),
        S("1-4", "検索する",
          "<b>検索</b> を押します。見つかった件数が横に出て、一覧に画像が新しい順に並びます。"
          "0 件のときは、地図が画像のありそうな場所を映しているか、撮像日時の期間が短すぎないかを確かめます。",
          target=lambda: catalog.search_button, reveal=reveal(catalog, "画像カタログ"),
          begin=lambda: Counter(catalog.model.modelReset), done=searched, demo=catalog.search, ok="一覧に画像が並びます"),
        S("2-1", "画像を選ぶ",
          "一覧で、見たい画像の行をクリックします。地図に、その画像の写っている範囲が橙の枠で出ます。見たい場所に重なっているかを、ここで確かめます。",
          target=lambda: catalog.table, reveal=reveal(catalog, "画像カタログ"),
          done=lambda _b: bool(catalog.chosen()), demo=lambda: catalog.table.selectRow(0)),
        S("2-2", "開く",
          "<b>開く</b> を押します。行をダブルクリックしても開きます。"
          "<b>前の画像を閉じる</b> にチェックがあると、次の画像を開いたときに、前に開いた画像が地図から外れます。",
          target=lambda: catalog.buttons["open"], reveal=reveal(catalog, "画像カタログ"),
          begin=raster_ids, done=lambda before: before is not None and bool(raster_ids() - before), demo=open_first,
          ok="画像が地図に入り、地図がその画像の範囲へ移ります"),
        S("3-1", "観測記録のドックを出して、3 か所を確かめる",
          "ツールバーの <b>観測記録</b> を押します。画面の右に観測記録のドックが開きます。記録を始める前に、"
          "<b>保存先</b>（記録を保存する所。練習では練習用観測記録）、<b>画像</b>（いま開いた画像）、"
          "<b>分担の行</b>（だれがこの画像を担当しているか）を確かめます。前の画像で記録したモノは、地図に白抜きの印で出ます。",
          target=lambda: [obs.store_select, obs.image_select, obs.assignment_bar] if shown(obs) else p.button("観測記録"),
          done=lambda _b: shown(obs) and obs.book is not None and obs.book.loaded and obs.book.current is not None,
          demo=lambda: show(obs)),
        S("4-1", "クリック記録を押す",
          "<b>クリック記録</b>（R）を押します。ボタンが「クリック記録中」になり、この画像は自動で自分の担当になります。"
          "クリック記録中は、地図のクリックが記録になります。地図はホイールで拡大・縮小し、ホイールを押したままドラッグして動かします。",
          target=lambda: obs.record_button, reveal=reveal(obs, "観測記録"),
          done=lambda _b: obs.active, demo=lambda: obs.record_button.setChecked(True), ok="「分担: 自分が担当中」になります"),
        S("5-1", "拡大して、記録するモノをクリックする",
          "記録するモノ（赤い枠の船など）が見分けられるところまで拡大して、モノの上をクリックします。ドックに記録のフォームが開きます。",
          target=lambda: ship_rect() or canvas, done=lambda _b: form_new(), demo=click_ship, ok="ドックに記録のフォームが開きます"),
        S("6-1", "種別をたどる",
          "フォームのボタンを、大分類 → 種別 → 級 の順に押して種別をたどります。押した順に、上の <b>種別</b> の欄に「船舶 > 貨物船」のように入ります。"
          "押したボタンをもう一度押すと、その段の選択が外れます。",
          target=lambda: (obs.kind_levels or obs.kind_input) if obs.form.isVisible() else None, reveal=form_reveal,
          done=lambda _b: obs.form.isVisible() and bool(obs.kind_input.text().strip()), demo=choose_kind),
        S("6-2", "活動と確度を選ぶ",
          "活動のボタンを押すと、<b>活動</b> の欄に入ります。数字キー 1〜9 でも選べます。<b>確度</b> の ▼ で、どのくらい確かかを選びます。",
          target=lambda: [obs.chips_box, obs.activity_input, obs.confidence_select] if obs.form.isVisible() else None, reveal=form_reveal,
          done=lambda _b: obs.form.isVisible() and bool(obs.activity_input.text().strip()), demo=choose_activity),
        S("6-3", "備考を入れて記録する",
          "必要なら <b>備考</b> に気づいたことを書き、<b>記録</b>（Enter）を押します。記録は、その場で保存先に保存されます。やめるときは <b>取消</b>（Esc）です。",
          target=lambda: [obs.note_input, obs.submit_button] if obs.form.isVisible() else None, reveal=form_reveal,
          begin=lambda: obs.table.model().rowCount(),
          done=lambda n: n is not None and not obs.form.isVisible() and obs.table.model().rowCount() > n,
          demo=submit_new, ok="地図のモノに印と名前が付き、一覧に行が増えます"),
        S("7-1", "白抜きの印をクリックする",
          "白抜きの印は、前の画像で記録したモノです。この画像でもいるかを見て、印をクリックします。ドックにそのモノのフォームが開きます。",
          skip=lambda: "" if ghosts() or on_ghost() else no_ghost,
          target=ghost_rect, reveal=("白抜きの印へ地図を動かす", go_to_ghost), done=lambda _b: on_ghost(), demo=click_ghost),
        S("7-2", "継続か消失を押す",
          "まだいれば <b>継続</b>（Enter）、いなければ <b>消失</b> を押します。活動が変わっていれば、押す前に活動のボタンで選び直します。"
          "一覧の灰色の行の右端にも 継続・消失 のボタンがあります。",
          skip=lambda: "" if ghosts() or on_ghost() else no_ghost,
          target=lambda: [obs.submit_button, obs.absent_button] if on_ghost() else ghost_rect(),
          begin=lambda: len(ghosts()), done=lambda n: n is not None and not obs.form.isVisible() and len(ghosts()) < n, demo=carry),
        S("7-3", "まとめて継続・消失にする",
          "前の画像のモノの数は、この欄に出ます。全部まだいるなら <b>すべて継続</b>、全部いなくなっていれば <b>すべて消失</b> で、まとめて記録できます"
          "（練習では押さなくてもかまいません）。",
          target=lambda: [obs.carry_label, obs.carry_all_button, obs.absent_all_button], reveal=reveal(obs, "観測記録")),
        S("8-1", "一覧を見て、クリック記録を止める",
          "一覧で、この画像で記録したモノと、まだ確かめていない前の画像のモノ（灰色の行）を見分けます。灰色の行が残っていれば、7. と同じように継続か消失にします。"
          "全部終わったら、<b>クリック記録中</b> を押して（R）記録を止めます。",
          target=lambda: [obs.table, obs.record_button], reveal=reveal(obs, "観測記録"),
          done=lambda _b: not obs.active, demo=lambda: obs.record_button.setChecked(False)),
        S("9-1", "完了を押す",
          "見終わったら、分担の行の <b>完了</b> を押します。途中でやめて、ほかの人に任せるときは、完了 ではなく <b>手放す</b> を押します。",
          target=lambda: obs.assignment_bar.done_button if obs.assignment_bar.done_button.isVisible() else obs.assignment_bar,
          reveal=reveal(obs, "観測記録"), done=lambda _b: "完了" in obs.assignment_bar.label.text(), demo=done_assignment,
          ok="分担の行が「担当者なし（自分の名前 さんが完了済み）」に変わります"),
        S("9-2", "画像カタログでも完了を確かめる",
          "画像カタログで <b>検索</b> し直すと、その画像の <b>分担</b> の列が「完了（名前）」になっています。ほかの人にも、見終わった画像だと分かります。",
          target=lambda: catalog.search_button if shown(catalog) else p.button("画像カタログ"),
          begin=lambda: Counter(catalog.model.modelReset), done=searched, demo=lambda: (show(catalog), catalog.search())),
        S("9-3", "次の画像へ進む",
          "観測記録ドックの ◀（PageUp）と ▶（PageDown）で、開いている画像を撮像日時の順に切り替えます。真ん中の ▼ から選んでも切り替えられます。"
          "まだ開いていない画像は、画像カタログから 2. と同じように開きます。",
          target=lambda: [obs.prev_button, obs.image_select, obs.next_button], reveal=reveal(obs, "観測記録")),
    ], setup=setup)


# ---- 撮像を依頼して、計画に入ったか確かめる（撮像を依頼する人） -----------------------------------

def _form(requests, batch: bool = False):
    """The request form open now (of レイヤーから取り込み when `batch`), None when none."""
    form = requests.form
    try:
        if form is not None and form.isVisible() and (form.batch is not None) == batch:
            return form
    except RuntimeError:
        pass
    return None


def _choose(combo, text: str, starts: bool = False) -> None:
    for i in range(combo.count()):
        if combo.itemText(i) == text or (starts and combo.itemText(i).startswith(text)):
            combo.setCurrentIndex(i)
            return


def _set_level(form, word: str) -> None:
    for i in range(form.level.count()):
        if form.level.itemText(i).startswith(word):
            form.level.setCurrentIndex(i)
            value = form.level.itemData(i)
            if isinstance(value, int):
                form.priority.setValue(value)
            return


def request_course(p) -> Course:
    api, canvas = p.api, p.canvas
    requests, schedule, places = api.requests, api.schedule, api.place_schedule
    tomorrow = date.today() + timedelta(days=1)
    w, s, e, n = REQUEST_AREA

    def form():
        return _form(requests)

    def open_form() -> None:
        if form() is None:
            requests.edit(new=True)

    def name_group() -> None:
        open_form()
        f = form()
        f.name.setText(REQUEST_NAME)
        _choose(f.group, "第1班")

    def when() -> None:
        f = form()
        _choose(f.category, "臨時")
        _choose(f.achieve, "期間内1回")
        f.start.setDate(QDate.currentDate())
        f.end.setDate(QDate.currentDate().addDays(7))

    def area() -> None:
        open_form()
        fit(canvas, w - 0.03, s - 0.02, e + 0.03, n + 0.02)
        form().set_box(w, s, e, n)

    def save() -> None:
        f = form()
        if not f.requester.text().strip():
            f.requester.setText("練習")
        if not f.note.toPlainText().strip():
            f.note.setPlainText("岸壁の船の入れ替わりを確認したい（練習）")
        save_button(f).click()

    def saved(count) -> bool:
        return count is not None and form() is None and len(requests.requests) > count

    def choose_saved() -> None:
        rows = [i for i, r in enumerate(requests.shown) if r.name == REQUEST_NAME]
        if rows:
            requests.table.selectRow(rows[-1])

    def with_held_back() -> None:
        chosen = schedule.state_filter.chosen()
        if chosen and HELD_BACK not in chosen:
            schedule.state_filter.choose([*chosen, HELD_BACK])

    def setup() -> str:
        if p.builtin:
            p.to_sample()
        return ""

    form_reveal = ("依頼のフォームを開き直す", open_form)
    no_groups = "この練習の台帳には依頼グループの表がありません。"
    return Course("request", "撮像を依頼して、計画に入ったか確かめる", "REQUESTER",
                  "撮像依頼を登録 → 撮像予定で採否を見る → 地点ごとの撮像予定で次の撮像を見る（16 の手順）。",
                  "howto-request", [
        S("1-1", "撮像依頼のドックを出す",
          "ツールバーの <b>撮像依頼</b> を押します。画面の右に撮像依頼のドックが開き、登録済みの依頼が一覧に並びます。",
          target=lambda: requests.table if shown(requests) else p.button("撮像依頼"),
          done=lambda _b: shown(requests) and requests.loaded and requests.task is None, demo=lambda: show(requests)),
        S("1-2", "自分のグループの依頼だけにする",
          "グループの欄の ▼ を押して、<b>自分のグループ</b> にチェックを入れます。一覧が自分のグループの依頼だけになります。選んだものは次回も同じです。",
          skip=lambda: "" if requests.group_filter.isVisible() or not shown(requests) else no_groups,
          target=lambda: requests.group_filter, reveal=reveal(requests, "撮像依頼"),
          done=lambda _b: MINE in requests.group_filter.chosen(), demo=lambda: requests.group_filter.choose(MINE)),
        S("1-3", "新しい依頼のフォームを開く",
          "<b>新規…</b> を押します。依頼のフォームが開きます。新しい依頼は、前回保存した依頼の条件から始まります（名称・期間・範囲は毎回空です）。",
          target=lambda: requests.new_button, reveal=reveal(requests, "撮像依頼"), ready=lambda: requests.loaded,
          done=lambda _b: form() is not None, demo=open_form, ok="依頼のフォームが開きます"),
        S("2-1", "名称とグループを入れる",
          f"<b>名称</b> に、あとで一覧や撮像予定で見分けられる名前を入れます（例: {REQUEST_NAME}）。"
          "<b>管理番号・識別番号</b> は、職場で番号を付けているときだけ入れます。<b>依頼グループ</b> で自分のグループを選びます。",
          target=lambda: [form().name, form().group] if form() else None, reveal=form_reveal,
          done=lambda _b: form() is not None and bool(form().name.text().strip()), demo=name_group),
        S("2-2", "区分・達成条件・期間を入れる",
          "<b>区分</b> と <b>達成条件</b> で、どう撮るかを選びます。期間中に 1 回撮ればよいなら「臨時」と「期間内1回」、毎日撮るなら「定点」と「毎回」です。"
          "<b>期間</b> には、撮ってほしい最初と最後の日（日本時間）を入れます。",
          target=lambda: [form().category, form().achieve, form().start, form().end] if form() else None, reveal=form_reveal,
          demo=when),
        S("2-3", "優先度を選ぶ",
          "<b>優先度</b> の ▼ を押して、5 段階から選びます。上ほど優先されます。数値で細かく決めるときは、横の 詳細 で 1〜1000 を入れます（1 ほど高い）。",
          target=lambda: form().level if form() else None, reveal=form_reveal, demo=lambda: _set_level(form(), "高")),
        S("2-4", "撮り方の条件を選ぶ",
          "空欄・「任意」・「指定なし」のままなら、どれでもよいという意味です。必要なところだけ入れます。"
          "<b>種別</b> は 光学（ふつうの写真。雲があると写らない）・SAR（電波。夜・雲でも撮れる）・任意。<b>衛星</b> は撮る衛星を決めるときだけチェックします。",
          target=lambda: [form().kind, form().tree] if form() else None, reveal=form_reveal,
          demo=lambda: form().kind.setCurrentText("光学")),
        S("2-5", "範囲を決める",
          "<b>地図で矩形</b> を押して、地図の上で囲みたい範囲の角から反対の角までドラッグします。形を描くときは <b>地図で多角形</b>、"
          "地点で決めるときは <b>地図で点</b>、レイヤーの地物の形を使うときは <b>選択中の地物</b> です。"
          "範囲を決めると、その下に、期間内に衛星が撮れる回数が出ます。赤で「撮像機会なし」のときは、期間を延ばすか種別を任意にします。",
          target=lambda: [form().box_button, form().polygon_button, form().selected_button] if form() else None, reveal=form_reveal,
          done=lambda _b: form() is not None and bool(form().rings), demo=area, ok="範囲の下に、撮れる回数が出ます"),
        S("2-6", "依頼者・メモを入れて保存する",
          "<b>依頼者</b> に自分の名前、<b>メモ</b> に撮りたい理由など、計画の担当に伝えたいことを書きます。<b>保存</b> を押します。"
          "保存できないときは、フォームの下に赤い字で理由が出ます。",
          target=lambda: [form().requester, form().note, save_button(form())] if form() else None, reveal=form_reveal,
          begin=lambda: len(requests.requests), done=saved, demo=save, ok="保存した依頼が一覧に入ります"),
        S("3-1", "保存した依頼を見る",
          "保存した依頼が一覧に入ります。これで依頼は終わりです。直すときは、行を選んで <b>編集…</b> を押し、2. と同じフォームで直して保存します。",
          target=lambda: [requests.table, requests.edit_button], reveal=reveal(requests, "撮像依頼"), demo=choose_saved),
        S("5-1", "撮像予定のドックを出す",
          "計画に入ったかは、撮像計画の担当が計算・書き出しをしたあとに見ます。ツールバーの <b>撮像予定</b> を押します。"
          "上にカレンダー、下にその日の計画の一覧が出ます。",
          target=lambda: [schedule.calendar, schedule.table] if shown(schedule) else p.button("撮像予定"),
          done=lambda _b: shown(schedule) and schedule.store is not None, demo=lambda: show(schedule)),
        S("5-2", "日を選ぶ",
          "カレンダーで見たい日をクリックします。計画のある日は太字で出ます（緑の太字は計画に入ったものがある日、青の太字は採否がまだ分からないものだけの日）。"
          "練習では、「撮像計画を計算して出し、採否を入れる」のコースで書き出した日に計画が入ります。",
          target=lambda: schedule.calendar, reveal=reveal(schedule, "撮像予定"),
          begin=lambda: schedule.day, done=lambda day: bool(schedule.day) and schedule.day != day and schedule.task is None,
          demo=lambda: schedule.set_day(tomorrow)),
        S("5-3", "自分のグループの依頼だけにする",
          "一覧の上のグループの欄の ▼ を押して、<b>自分のグループ</b> にチェックを入れます。",
          skip=lambda: "" if schedule.group_filter.isVisible() or not shown(schedule) else no_groups,
          target=lambda: schedule.group_filter, reveal=reveal(schedule, "撮像予定"),
          done=lambda _b: MINE in schedule.group_filter.chosen(), demo=lambda: schedule.group_filter.choose(MINE)),
        S("6-1", "自分の依頼の行を読む",
          "自分の依頼の行の <b>状態</b> を見ます。<b>採用</b>: システムの計画に入った。<b>不明</b>: システムの返事がまだない。"
          "<b>不採用</b>: 計画に入らなかった（理由は 理由・メモ に）。<b>見送り</b>: その日はシステムに出さなかった。"
          "<b>まとめ</b> の列は、近くの依頼と 1 回の撮像でまとめて撮ることを表します。",
          target=lambda: schedule.table, reveal=reveal(schedule, "撮像予定")),
        S("6-2", "見送りも表示する",
          "最初は、見送りは一覧に出ません。見送りも見るときは、状態の欄の ▼ を押して <b>見送り</b> にチェックを入れます。",
          target=lambda: schedule.state_filter, reveal=reveal(schedule, "撮像予定"),
          done=lambda _b: schedule.state_filter.is_all() or HELD_BACK in schedule.state_filter.chosen(), demo=with_held_back),
        S("7-1", "地点を足して、タイムラインを見る",
          "ツールバーの <b>地点ごとの撮像予定</b> を押します。<b>地図で地点を追加</b> を押し、地図で依頼した場所をクリックします。"
          "その地点の計画がタイムラインに日ごとに並び、名前の下に「次」の撮像（日時・状態・衛星）が出ます。",
          target=lambda: places.pick if shown(places) else p.button("地点ごとの撮像予定"),
          begin=lambda: len(places.lanes), done=lambda count: count is not None and len(places.lanes) > count,
          demo=lambda: (show(places), places.add_point((w + e) / 2, (s + n) / 2, "岸壁の沖")), ok="タイムラインに地点の計画が並びます"),
    ], setup=setup)


# ---- たくさんの場所をまとめて依頼する（レイヤーから取り込み） ---------------------------------------

def places_layer() -> QgsVectorLayer:
    """The requester's own layer of places to watch (as in the help's pictures), added once."""
    for layer in QgsProject.instance().mapLayersByName(PLACES_LAYER):
        if isinstance(layer, QgsVectorLayer):
            return layer
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", PLACES_LAYER, "memory")
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
    QgsProject.instance().addMapLayer(layer)
    return layer


def layer_course(p) -> Course:
    api, canvas = p.api, p.canvas
    requests = api.requests
    kept: dict = {}

    def dialog():
        d = requests.import_dialog
        try:
            return d if d is not None and d.isVisible() else None
        except RuntimeError:
            return None

    def setup() -> str:
        layer = places_layer()
        kept["layer"] = layer
        p.iface.setActiveLayer(layer)
        show(requests)
        xs = [x for _n, _k, ring in PLACES for x, _y in ring]
        ys = [y for _n, _k, ring in PLACES for _x, y in ring]
        fit(canvas, min(xs) - 0.02, min(ys) - 0.01, max(xs) + 0.02, max(ys) + 0.01)
        return f"練習用に、見たい場所のレイヤー「{PLACES_LAYER}」（紫の形 {len(PLACES)} か所）を地図に入れました。"

    def start() -> None:
        layer = kept.get("layer") or places_layer()
        if requests.import_layer(layer) is None:
            raise ValueError(requests.status.text() or "撮像依頼の台帳を読み込んでから取り込みます")

    def fields() -> None:
        d = dialog()
        if d is None:
            start()
            d = dialog()
        layer = kept.get("layer") or places_layer()
        d.layer.setLayer(layer)
        d.name_field.setField("名称")
        d.management_field.setField("管理番号")

    def go_next() -> None:
        if dialog() is None:
            fields()
        dialog().ok.click()

    def rest() -> None:
        f = _form(requests, batch=True)
        if f is None:
            go_next()
            f = _form(requests, batch=True)
        _choose(f.group, "第1班")
        f.start.setDate(QDate.currentDate())
        f.end.setDate(QDate.currentDate().addDays(30))
        _set_level(f, "中")
        f.kind.setCurrentText("光学")
        if not f.requester.text().strip():
            f.requester.setText("練習")
        save_button(f).click()

    def batch():
        return _form(requests, batch=True)

    return Course("layer", "たくさんの場所をまとめて依頼する（レイヤーから取り込み）", "REQUESTER",
                  "港・岸壁の一覧のように、依頼したい場所がレイヤーになっているとき、地物ごとに 1 件の依頼をまとめて作る（4 の手順）。",
                  "howto-request", [
        S("4-1", "レイヤーを入れて、取り込みを始める",
          "場所のレイヤーを QGIS に入れておき（地図に紫の形で出ているもの）、撮像依頼ドックの <b>レイヤーから取り込み…</b> を押します。",
          target=lambda: requests.import_button, reveal=reveal(requests, "撮像依頼"),
          ready=lambda: requests.loaded and requests.task is None, done=lambda _b: dialog() is not None, demo=start),
        S("4-2", "レイヤーと属性を選ぶ",
          "取り込む <b>レイヤー</b> を選びます。依頼の名称にする属性（練習では「名称」）を選びます。管理番号にする属性があれば、それも選びます（「管理番号」）。",
          target=lambda: [dialog().layer, dialog().name_field, dialog().management_field] if dialog() else None,
          reveal=("取り込みの窓を開き直す", start), demo=fields),
        S("4-3", "取り込み方を確かめて次へ進む",
          "一部の地物だけにするときは、先に地図で地物を選んでから <b>選択中の地物のみ</b> にチェックを入れます。点の地物は、点の範囲の一辺の正方形が範囲になります。"
          "<b>範囲・名称・番号だけ取り込み…</b> のまま <b>次へ</b> を押します。",
          target=lambda: [dialog().selected_only, dialog().point_km, dialog().simple, dialog().ok] if dialog() else None,
          reveal=("取り込みの窓を開き直す", start), done=lambda _b: batch() is not None, demo=go_next,
          ok="依頼のフォームが「レイヤーから取り込み」として開きます"),
        S("4-4", "残りの項目を 1 回だけ入れて保存する",
          "<b>名称</b> には地物ごとの名前が入っています。区分・期間・優先度・条件など残りの項目は、1 件の依頼と同じように 1 回だけ入れます。"
          "全部の地物に同じ値が入ります。<b>範囲</b> の欄に取り込む地物の数が出るので、確かめて <b>保存</b> を押します。",
          target=lambda: [batch().area_label, save_button(batch())] if batch() else None,
          begin=lambda: len(requests.requests),
          done=lambda count: count is not None and batch() is None and len(requests.requests) > count, demo=rest,
          ok="状況欄に「3 件を取り込み」と出て、地物の数だけ依頼が一覧に入ります"),
    ], setup=setup)


# ---- 撮像計画を計算して出し、採否を入れる（撮像計画の担当） ---------------------------------------

def plan_course(p) -> Course:
    api, canvas = p.api, p.canvas
    files, schedule = api.plan_files, api.schedule
    kept: dict = {"answers": []}

    def plan_day() -> date:
        return files.day.date().toPyDate()

    def calculate() -> None:
        show(files)
        files.day.setDate(QDate.currentDate().addDays(1))
        files.run()

    def calculated(before) -> bool:
        return files.plan is not None and files.task is None and id(files.plan) != before

    def on_map() -> None:
        files.on_map.setChecked(True)
        files.draw()

    def write() -> None:
        QTimer.singleShot(0, lambda: files.write(ask=True))

    def answer_yes() -> None:
        yes = question_yes()
        if yes is not None:
            yes.click()
        else:
            files.write(ask=False)

    def open_day() -> None:
        show(schedule)
        schedule.group_filter.choose([])
        schedule.set_day(plan_day())

    def systems() -> list:
        return [x for x in api.config.request_exports if x.result_file is not None]

    def answered() -> int:
        return sum(1 for r in schedule.rows if r.adoption in (PLANNED, DECLINED))

    def answer_files() -> int:
        """The systems' answers to the rows of the day shown, made in the practice's folder."""
        made = []
        for system in systems():
            rows = handed_out(schedule.rows, system.system)
            data = answer_bytes(system.result_file, rows)
            if not data:
                continue
            folder = os.path.join(p.outputs, ANSWERS_FOLDER)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{system.system}_採否_{schedule.day}.{'json' if system.result_file.format == 'json' else 'csv'}")
            with open(path, "wb") as out:
                out.write(data)
            made.append((system, path))
        kept["answers"] = made
        return answered()

    def no_answer() -> str:
        if not systems():
            return "この練習の衛星システムには、返ってくる採否のファイルの決まり（requestExports の resultFile）がありません。5. の手入力で練習します。"
        if not schedule.day or not any(handed_out(schedule.rows, x.system) for x in systems()):
            return "撮像予定で開いている日に、書き出した依頼がありません（3. で書き出すと、ここで採否を入れられます）。"
        return ""

    def answer_text() -> str:
        where = "、".join(path for _s, path in kept["answers"]) or "（作れませんでした）"
        return ("システムから採否のファイルが返ってきたら、<b>結果ファイルの取り込み…</b> を押してそのファイルを選びます。"
                f"練習用に、システムから返ってきたファイルを作りました: <b>{where}</b><br>"
                "読み取った採用・不採用の件数が出たら、確かめて <b>はい</b> を押します。")

    def import_answer() -> None:
        if not kept["answers"]:
            raise ValueError("取り込むファイルがありません")
        system, path = kept["answers"][0]
        schedule.import_file(path=path, system=system, ask=False)

    def by_hand_rows() -> list:
        return [r for r in schedule.shown if r.adoption == AWAITING] or list(schedule.shown[-1:])

    def by_hand() -> None:
        rows = by_hand_rows()
        if not rows:
            raise ValueError("一覧に行がありません")
        row = rows[0]
        schedule.table.selectRow(schedule.shown.index(row))
        schedule.reason.setText("電話で確認（練習）")
        schedule.answer(DECLINED, rows=[row])

    def snapshot() -> dict:
        return {(r.key, r.part, r.system): (r.adoption, r.adoption_reason) for r in schedule.rows}

    def answered_by_hand(before) -> bool:
        if before is None or schedule.task is not None:
            return False
        return any(r.adoption_source == MANUAL and before.get((r.key, r.part, r.system)) != (r.adoption, r.adoption_reason)
                   for r in schedule.rows)

    def setup() -> str:
        if p.builtin:
            p.to_sample()
        return ""

    return Course("plan", "撮像計画を計算して出し、採否を入れる", "PLANNER",
                  "撮像計画ファイル出力で計算 → 調整システムに渡すファイルを書き出す → 返ってきた採否を入れる（8 の手順）。",
                  "howto-plan", [
        S("1-1", "対象日を選んで計算する",
          "ツールバーの <b>撮像計画ファイル出力</b> を押してドックを出します。<b>対象日</b> に計画を作る日（ふつうは明日）を選び、<b>計算</b> を押します。",
          target=lambda: [files.day, files.run_button] if shown(files) else p.button("撮像計画ファイル出力"),
          begin=lambda: id(files.plan) if files.plan is not None else None, done=calculated, demo=calculate,
          ready=lambda: files.store is not None and files.task is None,
          ok="状況欄に候補・採用・見送りの件数が出て、依頼が一覧に並びます"),
        S("2-1", "列を読み、採用を変える",
          "<b>採用</b> の列にチェックが入った行がファイルに出ます。チェックを外すと見送り、入れると枠を超えても採用になります。"
          "<b>結果・理由</b> の列に、「採用：まとめ 2 件」「見送り：撮像機会なし」など、そうなった理由が出ます。",
          target=lambda: files.table, reveal=reveal(files, "撮像計画ファイル出力")),
        S("2-2", "地図で確かめる",
          "<b>地図に表示</b> にチェックを入れます。依頼の範囲（採用は緑、まとめは青、見送りは灰）と撮像の範囲（紫の破線）が地図に出ます。行を選ぶと、その依頼へ地図が移ります。",
          target=lambda: files.on_map, reveal=reveal(files, "撮像計画ファイル出力"),
          done=lambda _b: files.on_map.isChecked(), demo=on_map),
        S("3-1", "出力先を確かめて書き出す",
          f"<b>出力先</b> のフォルダを確かめて、<b>書き出し</b> を押します。練習では、出力先は練習用のフォルダ（{os.path.join(p.outputs, '撮像計画ファイル')}）です。",
          target=lambda: [files.folder, files.write_button], reveal=reveal(files, "撮像計画ファイル出力"),
          begin=lambda: list(files.paths), done=lambda before: question_yes() is not None or (before is not None and list(files.paths) != before),
          demo=write, ok="ファイル名と件数を聞かれます"),
        S("3-2", "ファイル名と件数を確かめる",
          "システムごとのファイル名と件数が出ます。確かめて <b>はい</b> を押します。",
          target=lambda: question_yes() or files.status, begin=lambda: list(files.paths),
          done=lambda before: question_yes() is None and files.task is None and before is not None and bool(files.paths) and list(files.paths) != before,
          demo=answer_yes, ok="状況欄に書き出した件数が出ます。書き出したファイルを調整システムに渡します"),
        S("4-1", "撮像予定で対象日を開く",
          "ツールバーの <b>撮像予定</b> を押します。カレンダーで対象日を選びます。書き出したばかりの依頼は、一覧で「不明」です。",
          target=lambda: schedule.calendar if shown(schedule) else p.button("撮像予定"),
          done=lambda _b: shown(schedule) and schedule.day == plan_day().isoformat() and schedule.task is None, demo=open_day),
        S("4-2", "返ってきた採否を取り込む", answer_text,
          skip=no_answer, target=lambda: question_yes() or schedule.import_button, reveal=reveal(schedule, "撮像予定"),
          begin=answer_files, done=lambda count: count is not None and schedule.task is None and answered() > count,
          demo=import_answer, ok="ファイルにあった依頼が「採用」になります。ファイルになかった依頼は「不明」のままです"),
        S("5-1", "電話などで聞いた採否を入れる",
          "「不明」のままの行を選びます。必要なら <b>理由</b> を書き、<b>不採用</b> か <b>採用</b> を押します。"
          "間違えたときは、行を選んで <b>不明に戻す</b> を押します。",
          target=lambda: [schedule.table, schedule.reason, schedule.reject_button], reveal=reveal(schedule, "撮像予定"),
          begin=snapshot, done=answered_by_hand, demo=by_hand, ok="行の状態が変わり、理由が 理由・メモ に入ります"),
    ], setup=setup)


def courses(place) -> list[Course]:
    """Every course, in the order of the help (「はじめての MIKAGE」 first)."""
    makers: list[Callable[[Any], Course]] = [first_course, obs_course, request_course, layer_course, plan_course]
    return [make(place) for make in makers]
