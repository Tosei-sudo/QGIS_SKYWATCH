"""
The 「メモ」 column of the request lists (tsudo, 2026-11-24: 「撮像依頼系のテーブル画面に、メモ列を表示できるようにしてほしい。
通常は非表示で」): 撮像依頼, 撮像予定, 撮像計画ファイル出力 and 計画と実績の照合 show the request's note, hidden until shown;
the list views' columns hidden until shown (ui/list_views.py `hidden`); 撮像予定's 「理由・メモ」 is 「理由」.
"""
import time

import pytest

pytest.importorskip("qgis.core")
from qgis.testing import start_app  # noqa: E402

APP = start_app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel  # noqa: E402
from qgis.PyQt.QtWidgets import QTableView  # noqa: E402

from mikage.core.request.model import jst_day  # noqa: E402
from mikage.core.request.store import DEFAULT_HISTORY_FIELDS, HistoryRow, history_attributes  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_request_groups import LEDGER, TOKYO, plugin  # noqa: E402,F401
from mikage.ui.list_views import ROOT, ColumnsDialog, ListViews, column_menus  # noqa: E402
from mikage.ui.memo_column import MEMO, MEMO_CHARS, memo_item, memo_text  # noqa: E402

HEAD = ["名称", "期間", "状態", "メモ"]
LONG = "東側の岸壁を優先。\n雲が多い日は翌日に回す（担当: 佐藤）。夜間は不要、朝の便で十分。ほかの依頼と重なるときは相談"


@pytest.fixture(autouse=True)
def clean():
    QgsSettings().remove(ROOT)
    yield
    QgsSettings().remove(ROOT)


def settle():
    for _ in range(3):
        APP.processEvents()


def table(head=HEAD):
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(head)
    model.appendRow([QStandardItem(t) for t in ("a", "", "有効", "メモ")])
    view = QTableView()
    view.setModel(model)
    return model, view


def shown(views):
    return [c["title"] for c in views.columns() if not c["hidden"]]


def column(model, title):
    return [model.horizontalHeaderItem(c).text() for c in range(model.columnCount())].index(title)


def test_a_note_on_one_line_cut_after_its_first_characters():
    assert memo_text("") == "" and memo_text(None) == ""
    assert memo_text("  東側\n\n  西側 ") == "東側 西側"
    cut = memo_text(LONG)
    assert len(cut) == MEMO_CHARS + 1 and cut.endswith("…") and "\n" not in cut
    assert cut.startswith("東側の岸壁を優先。 雲が多い日")
    item = memo_item(LONG)
    assert item.text() == cut and item.toolTip() == LONG
    # Others' text never read as HTML.
    assert "&lt;b&gt;" in memo_item("<b>太字</b>").toolTip()
    assert memo_item("").toolTip() == ""


def test_a_column_hidden_until_shown_and_kept_once_shown():
    model, view = table()
    views = ListViews("memo-test", view, hidden=("メモ",))
    assert shown(views) == ["名称", "期間", "状態"]
    assert [c["title"] for c in views.columns()] == HEAD
    # Ticked in the header's menu: shown, and kept when the columns are made again.
    menu = views.columns_menu()
    tick = next(a for a in menu.actions() if a.text() == "メモ")
    assert not tick.isChecked()
    tick.setChecked(True)
    assert shown(views) == HEAD
    model.clear()
    model.setHorizontalHeaderLabels(HEAD)
    settle()
    assert shown(views) == HEAD
    # 「標準の表示に戻す」 hides it again; 「列の表示と順番…」's 「標準に戻す」 too.
    views.reset()
    assert shown(views) == ["名称", "期間", "状態"]
    views.set_hidden("メモ", False)
    dialog = ColumnsDialog(views)
    assert [shown for _i, shown in dialog.chosen()] == [True, True, True, True]
    dialog.standard()
    assert [shown for _i, shown in dialog.chosen()] == [True, True, True, False]
    dialog.accept_into()
    assert shown(views) == ["名称", "期間", "状態"]
    # Kept per list: another list of the same key starts as left.
    views.set_hidden("メモ", False)
    _model2, view2 = table()
    again = column_menus("memo-test", view2, hidden=("メモ",))
    assert shown(again) == HEAD


def test_an_arrangement_or_a_view_kept_before_the_column_leaves_it_hidden():
    model, view = table(HEAD[:3])
    views = ListViews("memo-old", view)
    views.set_hidden("期間", True)
    views.save("昔のビュー")
    # The list gains the column (a newer plugin): hidden where the kept arrangement does not name it.
    model2, view2 = table()
    views2 = ListViews("memo-old", view2, hidden=("メモ",))
    assert shown(views2) == ["名称", "状態"]
    assert views2.choose("昔のビュー")
    assert shown(views2) == ["名称", "状態"]
    # A list without such columns: every column it does not name shown, as before.
    _model, view = table(HEAD[:3])
    ListViews("memo-plain", view).set_hidden("期間", True)
    model3, view3 = table()
    views3 = ListViews("memo-plain", view3)
    assert shown(views3) == ["名称", "状態", "メモ"]
    # Never every column hidden, even when the list hides its only column until shown.
    model4 = QStandardItemModel()
    model4.setHorizontalHeaderLabels(["メモ"])
    view4 = QTableView()
    view4.setModel(model4)
    only = ListViews("memo-only", view4, hidden=("メモ",))
    assert shown(only) == ["メモ"]
    only.reset()
    assert shown(only) == ["メモ"]


def test_the_request_lists_show_the_note_once_the_column_is_ticked(plugin):  # noqa: F811
    p, fake, history = plugin
    notes = {"東京湾": LONG, "大阪湾": "南側だけ"}
    for row in fake.layers[LEDGER].rows:
        row["attributes"]["NOTE"] = notes.get(row["attributes"]["REQ_NAME"])

    # 撮像依頼: the column at the end, hidden; its cell the note on one line, the whole note its tip.
    dock = p.api.requests
    dock.show()
    assert wait_for(lambda: dock._loaded and dock.task is None), dock.status.text()
    settle()
    col = column(dock.model, MEMO)
    assert col == dock.model.columnCount() - 1 and dock.table.isColumnHidden(col)
    row = next(i for i, r in enumerate(dock.shown) if r.name == "東京湾")
    assert dock.model.item(row, col).text() == memo_text(LONG) and dock.model.item(row, col).toolTip() == LONG
    assert dock.model.item(row, 0).toolTip() == LONG
    other = next(i for i, r in enumerate(dock.shown) if r.name == "横浜")
    assert dock.model.item(other, col).text() == ""
    assert dock.views.set_hidden(MEMO, False) and not dock.table.isColumnHidden(col)
    dock.render()
    settle()
    assert not dock.table.isColumnHidden(col)
    # The status colour stays on 状態.
    assert column(dock.model, "状態") == col - 1

    # 撮像計画ファイル出力: the request's note; 繰り上げ still the column to tick for a raise.
    daily = p.api.plan_files
    daily.show()
    daily.run()
    assert wait_for(lambda: daily.plan is not None and daily.task is None), daily.status.text()
    settle()
    col = column(daily.model, MEMO)
    assert daily.table.isColumnHidden(col)
    texts = {daily.model.item(r, column(daily.model, "依頼")).text(): daily.model.item(r, col).text() for r in range(daily.model.rowCount())}
    assert texts["大阪湾"] == "南側だけ" and texts["横浜"] == ""
    assert any(t.startswith("東京湾") and v == memo_text(LONG) for t, v in texts.items())
    from mikage.ui.plan_file_dock import BOOST_COLUMN, COLUMNS
    assert COLUMNS[BOOST_COLUMN] == "繰り上げ" and BOOST_COLUMN != col

    # 撮像予定: 「理由」 (not 「理由・メモ」) and the request's note from the ledger; 要求外 rows none.
    today = jst_day(time.time() * 1000).isoformat()
    now = time.time() * 1000
    for rid, name in (("R-1", "東京湾"), ("R-2", "大阪湾"), ("", "電話の計画")):
        row = HistoryRow("RUN1", now, today, rid, system="SYS-A", result="要求外" if not rid else "採用", name=name,
                         captured="未判定", adoption="採用" if not rid else "不明", adoption_reason="電話で" if not rid else "",
                         rings=TOKYO)
        history.add({k: v for k, v in history_attributes(row, DEFAULT_HISTORY_FIELDS).items() if v is not None},
                    {"rings": TOKYO, "spatialReference": {"wkid": 4326}})
    schedule = p.api.schedule
    schedule.show()
    wait_for(lambda: schedule.task is None)
    schedule.reload(refresh=True)
    assert wait_for(lambda: schedule._loaded and schedule.task is None and len(schedule.rows) == 3), schedule.status.text()
    settle()
    heads = [schedule.model.horizontalHeaderItem(c).text() for c in range(schedule.model.columnCount())]
    assert "理由・メモ" not in heads and heads[-2:] == ["理由", MEMO]
    col = heads.index(MEMO)
    assert schedule.table.isColumnHidden(col) and not schedule.table.isColumnHidden(heads.index("理由"))
    by_name = {schedule.model.item(r, 1).text(): r for r in range(schedule.model.rowCount())}
    assert schedule.model.item(by_name["東京湾"], col).toolTip() == LONG
    assert schedule.model.item(by_name["大阪湾"], col).text() == "南側だけ"
    assert schedule.model.item(by_name["電話の計画"], col).text() == ""
    assert schedule.model.item(by_name["電話の計画"], heads.index("理由")).text() == "電話で"

    # 計画と実績の照合: the request's note.
    results = daily.show_results(run=False)
    results.run(follow=False)
    assert wait_for(lambda: results.task is None and results.loaded is not None), results.status.text()
    settle()
    col = column(results.model, MEMO)
    assert results.table.isColumnHidden(col)
    texts = {results.model.item(r, column(results.model, "依頼")).text(): results.model.item(r, col).text()
             for r in range(results.model.rowCount())}
    assert texts == {"東京湾": memo_text(LONG), "大阪湾": "南側だけ"}
    results.close()
