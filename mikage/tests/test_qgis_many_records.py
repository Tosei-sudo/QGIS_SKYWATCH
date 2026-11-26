"""
Many records on one image (tsudo 2026-11-26: 観測記録が 100 件以上でカクつく):
the records' marks painted once as a picture and pasted while the map is
dragged, only what follows the pointer painted at each move; marks far off
the map not painted; the list filled in one go, its buttons painted (no
widget per row) and answering a click as the tool buttons did; the flows and
the classification worked out once per showing.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.PyQt.QtCore import QPoint, QPointF, QRectF, QSize, Qt  # noqa: E402
from qgis.PyQt.QtGui import QImage, QPainter  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QHBoxLayout, QHeaderView, QTableWidget, QTableWidgetItem, QToolButton, QWidget  # noqa: E402

from mikage.tests.test_qgis_observations import plugin  # noqa: E402,F401 (the fixture)
from mikage.ui.obs_overlay import Mark, Scene, ScenePainter  # noqa: E402
from mikage.ui.row_buttons import CHECKED_STYLE, RowButton, RowButtons  # noqa: E402


def _entry(i: int, n: int, carry=None):
    side = int(n ** 0.5) + 1
    return SimpleNamespace(lon=139.62 + 0.16 * (i % side) / side, lat=35.52 + 0.16 * (i // side) / side, object="", kind="車両 > 乗用車",
                           activity="駐車", confidence="", note="", length=4.5, heading=30.0, width=1.8, carry=carry)


def _many(dock, n: int):
    """`n` things on the first image, continued on the second (shown), and 5 more carried over."""
    book = dock.book
    first = book.add_many([_entry(i, n) for i in range(n + 5)])
    dock.step(1)
    book.add_many([_entry(i, n, carry=first[i]) for i in range(n)])
    return book


def _paint(overlay, size=QSize(800, 600)) -> QImage:
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    overlay.paint(painter)
    painter.end()
    return image


def test_the_records_picture_is_pasted_while_only_the_pointer_moves(plugin):
    p, iface, dock, layers, _ = plugin
    iface.canvas.resize(QSize(800, 600))
    book = _many(dock, 60)
    dock.render()
    overlay = dock.overlay
    first = _paint(overlay)
    picture = overlay._kept[1]
    assert picture is not None and len([m for _p, m in overlay.painted if m.role == "obs"]) == 60
    # Measuring by two points: the line follows the pointer, the records' picture stays as it was.
    dock.set_active(True)
    dock.rear = (139.70, 35.60)
    for i in range(5):
        dock.map_hovered(139.70 + i * 0.001, 35.601)
        after = _paint(overlay)
        assert overlay._kept[1] is picture
    assert overlay.live_.first == (139.70, 35.60) and overlay.live_.cursor == pytest.approx((139.704, 35.601))
    assert after != first  # the line is painted over it
    # Drawn again with nothing changed (render): still the same picture.
    dock.rear = dock.cursor = None
    dock.render()
    _paint(overlay)
    assert overlay._kept[1] is picture
    # A record changed: painted anew, its label as it is now.
    target = book.here()[0]
    dock.do("編集", lambda b: b.update(target.id, activity="航行"))
    _paint(overlay)
    assert overlay._kept[1] is not picture
    assert any(m.label.endswith("・航行") for _p, m in overlay.painted if m.key == target.id)
    # The map moved (its extent): painted anew where the records are now.
    picture = overlay._kept[1]
    iface.canvas.setExtent(layers[1].extent().buffered(0.05))
    _paint(overlay)
    assert overlay._kept[1] is not picture
    # Another style: painted anew too.
    picture = overlay._kept[1]
    dock.set_mark_style("outline")
    _paint(overlay)
    assert overlay._kept[1] is not picture


def test_a_record_dragged_follows_the_pointer_alone_and_stays_where_let_go(plugin):
    p, iface, dock, layers, _ = plugin
    iface.canvas.resize(QSize(800, 600))
    book = _many(dock, 10)
    dock.render()
    overlay = dock.overlay
    _paint(overlay)
    target = book.here()[2]
    mark = next(m for _p, m in overlay.painted if m.key == target.id)
    dock.map_dragged(mark, 139.75, 35.65, False)
    _paint(overlay)
    picture = overlay._kept[1]
    # Taken off the picture once, then only it moves.
    assert [m.key for m in overlay.live_.marks] == [target.id] and target.id not in {m.key for m in overlay.scene_.marks}
    dock.map_dragged(mark, 139.76, 35.66, False)
    _paint(overlay)
    assert overlay._kept[1] is picture and (overlay.live_.marks[0].lon, overlay.live_.marks[0].lat) == (139.76, 35.66)
    # Let go: saved there, back among the records.
    dock.map_dragged(mark, 139.76, 35.66, True)
    assert dock.dragging is None and book.record(target.id).lon == 139.76
    assert not overlay.live_.marks and any(m.key == target.id and m.lon == 139.76 for m in overlay.scene_.marks)
    # Hits still find the marks of the picture.
    _paint(overlay)
    at = next(p for p, m in overlay.painted if m.key == target.id)
    assert overlay.hit(at.x(), at.y()).key == target.id


def test_marks_far_off_the_map_are_not_painted_but_a_long_one_across_it_is():
    marks = [Mark("obs", 100, 100, label="中"), Mark("obs", 2000, 100, label="遠く"),
             # Its middle far off, its outline across the view (a ship at a close scale).
             Mark("obs", -500, 300, label="長い", outline=[(-1200, 280), (300, 280), (300, 320), (-1200, 320)], ends=((300, 300), (-1200, 300)))]
    image = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    scene_painter = ScenePainter(lambda x, y: QPointF(x, y))
    scene_painter.paint(painter, Scene(marks=marks), QRectF(0, 0, 400, 400))
    painter.end()
    assert [m.label for _p, m in scene_painter.painted] == ["中", "長い"]
    assert image.pixelColor(250, 300).alpha() > 0  # the long one's outline, inside the view
    # Without a view (a report's map): all of them.
    painter = QPainter(image)
    scene_painter.paint(painter, Scene(marks=marks))
    painter.end()
    assert len(scene_painter.painted) == 3


def test_the_list_is_filled_in_one_go_and_its_flows_worked_out_once(plugin, monkeypatch):
    p, iface, dock, layers, _ = plugin
    book = _many(dock, 40)
    calls = {"flows": 0, "taxonomy": 0, "carried": 0}
    for name in calls:
        real = getattr(book, name)

        def counted(*a, real=real, name=name, **k):
            calls[name] += 1
            return real(*a, **k)

        monkeypatch.setattr(book, name, counted)
    inserted = []
    dock.table.model().rowsInserted.connect(lambda _parent, first, last: inserted.append(last - first + 1))
    dock.render()
    # (The classification once more by the book itself, for the activities the form offers.)
    assert calls == {"flows": 1, "taxonomy": 2, "carried": 1}
    # One insertion of every row (not one per row), the room not needed dropped.
    assert inserted == [45] and dock.table.rowCount() == 45
    # Limited to another 大分類: the rows made room for and not filled are gone.
    dock.class_filter = ["船舶"]
    dock.render()
    assert dock.table.rowCount() == 1 and "該当する記録なし" in dock.table.item(0, 0).text()


def test_the_rows_buttons_are_painted_as_the_tool_buttons_were_and_answer_a_click(plugin):
    p, iface, dock, layers, _ = plugin
    book = _many(dock, 3)
    dock.render()
    table = dock.table
    buttons = dock.row_buttons
    names = [table.item(r, 0).text() for r in range(table.rowCount())]
    record_row = 0
    carried_row = next(r for r in range(table.rowCount()) if [b.text for b in buttons.of(r)] == ["継続", "消失"])
    assert [b.text for b in buttons.of(record_row)] == ["流れ"] and buttons.of(record_row)[0].checked is False
    # A press on a button neither chooses its row nor opens its record; its release does what it says.
    at = buttons.rects(record_row)[0].center()
    QTest.mousePress(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
    assert not table.selectionModel().selectedRows() and dock.chosen is None and not dock.form.isVisible()
    QTest.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
    assert dock.chosen == book.here()[0].object_id and dock.flow_box.isVisible()
    assert buttons.of(record_row)[0].checked is True and not dock.form.isVisible()
    # Pressed on one and let go off it: nothing.
    QTest.mousePress(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, buttons.rects(record_row)[0].center())
    QTest.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       table.visualRect(table.model().index(record_row, 1)).center())
    assert dock.chosen is not None
    # 「継続」 of a thing carried over records it on this image.
    here = len(book.here())
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, buttons.rects(carried_row)[0].center())
    assert len(book.here()) == here + 1
    # A click beside the buttons is a row's click as before.
    names = [table.item(r, 0).text() for r in range(table.rowCount())]
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     table.visualRect(table.model().index(1, 0)).center())
    assert dock.form.isVisible() and dock.object_input.text() == names[1]


@pytest.mark.parametrize("width", [420, 170, 140])
def test_the_painted_buttons_look_and_lie_as_the_tool_buttons_did(width):
    rows = [[("流れ", True)], [("流れ", False)], [("継続", None), ("消失", None)], [("復元", None)], [("削除", None)]]

    def table():
        t = QTableWidget()
        t.resize(width, 200)
        t.verticalHeader().hide()
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setColumnCount(3)
        t.setRowCount(len(rows))
        for r in range(len(rows)):
            for c, text in enumerate([f"車両-{r}", "駐車", ""]):
                t.setItem(r, c, QTableWidgetItem(text))
        return t

    # As the dock made them before: a box of tool buttons in each row's last cell.
    widgets = table()
    for r, spec in enumerate(rows):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        for text, on in spec:
            b = QToolButton()
            b.setText(text)
            if on is not None:
                b.setCheckable(True)
                b.setChecked(on)
                b.setStyleSheet(CHECKED_STYLE)
            lay.addWidget(b)
        widgets.setCellWidget(r, 2, box)
    painted = table()
    buttons = RowButtons(painted)
    for r, spec in enumerate(rows):
        buttons.set_row(r, [RowButton(text, "", lambda: None, on) for text, on in spec])
    for t in (widgets, painted):
        t.show()
    APP.processEvents()
    assert [widgets.columnWidth(c) for c in range(3)] == [painted.columnWidth(c) for c in range(3)]
    for r in range(len(rows)):
        box = widgets.cellWidget(r, 2)
        assert buttons.rects(r) == [b.geometry().translated(box.geometry().topLeft()) for b in box.findChildren(QToolButton)]
    assert widgets.viewport().grab().toImage() == painted.viewport().grab().toImage()
