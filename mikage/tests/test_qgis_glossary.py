"""備考の単語集 in the 観測記録 dock: read from the table, the words under the field while typing, Tab / Enter, 表記ゆれ fixed."""
import json

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QInputMethodEvent  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from mikage.core.esri import EsriClient  # noqa: E402
from mikage.core.obs.flows import living  # noqa: E402
from mikage.qgis_io import settings  # noqa: E402
from mikage.tests.fake_esri import FakeEsri, FakeLayer  # noqa: E402
from mikage.tests.qgis_fakes import wait_for  # noqa: E402
from mikage.tests.test_qgis_observations import plugin  # noqa: E402,F401 (the fixture)

URL = "https://example.com/arcgis/rest/services/Glossary/FeatureServer/0"
ROWS = [
    {"TERM": "入港", "VARIANTS": "入航,入湊", "READING": "にゅうこう", "ROOT_KIND": "船舶", "DESCRIPTION": "港に入る"},
    {"TERM": "入港直後", "ROOT_KIND": "船舶"},
    {"TERM": "停泊中"},
    {"TERM": "駐機", "ROOT_KIND": "航空機"},
]


def _glossary(p, tmp_path, rows=ROWS):
    fake = FakeEsri()
    layer = fake.add_layer(URL, FakeLayer({n: "esriFieldTypeString" for n in ("TERM", "VARIANTS", "READING", "ROOT_KIND", "DESCRIPTION")},
                                          geometry=""))
    for r in rows:
        layer.add(r)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"glossary": URL}), encoding="utf-8")
    settings.set_config_source(str(config))
    p.context.esri = lambda: EsriClient(fake)
    p.context.reload()
    assert wait_for(lambda: len(p.context.glossary.glossary) == len(rows))
    # The map drawn first: a test this short would otherwise end (and its process with it) while it draws, which
    # QGIS 3.34 does not survive (GDALClose at the canvas's deletion: "corrupted size vs. prev_size").
    assert wait_for(lambda: not p.iface.mapCanvas().isDrawing())
    return fake, layer


def _type(edit, text):
    """As typed with the IME: its text committed into the field (QTest.keyClicks takes only ASCII)."""
    edit.setFocus()
    event = QInputMethodEvent("", [])
    event.setCommitString(text)
    QApplication.sendEvent(edit, event)


def test_words_under_the_note_while_typing_tab_takes_the_first_enter_records(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    _glossary(p, tmp_path)
    assist = dock.note_assist
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    assert dock.form.isVisible()
    assert "単語集の語を候補に表示" in dock.note_input.placeholderText() and "4 語" in dock.note_input.toolTip()
    dock.choose_kind(["船舶"])
    _type(dock.note_input, "岸壁に入")
    popup = assist.completer.popup()
    assert popup.isVisible() and [s.term.text for s in assist.shown] == ["入港", "入港直後"]
    assert assist.model.item(0).text() == "入港　港に入る"
    # Nothing chosen at first; Tab takes the first word in place of what was typed.
    assert not popup.currentIndex().isValid()
    QTest.keyClick(popup, Qt.Key.Key_Tab)
    assert dock.note_input.text() == "岸壁に入港" and not popup.isVisible() and dock.form.isVisible()
    # ↓ then Enter takes the one chosen (and does not record).
    _type(dock.note_input, "、て")
    assert assist.shown == []
    _type(dock.note_input, "い")
    assert [s.term.text for s in assist.shown] == []  # 「てい」 is no reading here
    dock.note_input.setText("岸壁に入港、停")
    dock.note_input.textEdited.emit(dock.note_input.text())
    assert [s.term.text for s in assist.shown] == ["停泊中"] and popup.isVisible()
    QTest.keyClick(popup, Qt.Key.Key_Down)
    assert popup.currentIndex().isValid()
    QTest.keyClick(popup, Qt.Key.Key_Return)
    assert dock.note_input.text() == "岸壁に入港、停泊中" and dock.form.isVisible()
    # Another 大分類: its own words, not the ships'.
    dock.choose_kind(["航空機"])
    dock.note_input.setText("")
    _type(dock.note_input, "入")
    assert assist.shown == [] and not popup.isVisible()
    dock.note_input.setText("")
    _type(dock.note_input, "駐")
    assert [s.term.text for s in assist.shown] == ["駐機"]
    # Enter with nothing chosen goes to the field: the record is saved with the note as typed.
    QTest.keyClick(popup, Qt.Key.Key_Return)
    assert wait_for(lambda: not dock.form.isVisible())
    assert [o.note for o in living(dock.book.records)] == ["駐"]


def test_other_spellings_are_said_under_the_note_and_fixed(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    _glossary(p, tmp_path)
    assist = dock.note_assist
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    dock.note_input.setText("午前に入航、のち入湊")
    assist.check()
    assert assist.notice.isVisible() and assist.fix_button.isVisible()
    assert assist.label.text() == "表記ゆれ: 入航 → 入港、入湊 → 入港（単語集の表記）"
    assist.fix_button.click()
    assert dock.note_input.text() == "午前に入港、のち入港" and not assist.notice.isVisible()
    # Ctrl+Z takes it back, as typing would.
    dock.note_input.undo()
    assert dock.note_input.text() == "午前に入航、のち入湊"
    # Saving is not stopped.
    dock.submit()
    assert wait_for(lambda: [o.note for o in living(dock.book.records)] == ["午前に入航、のち入湊"])
    # The checked area's 備考 too.
    dock.area_note.setText("入航なし")
    dock.area_note_assist.check()
    assert dock.area_note_assist.variants[0].term == "入港"


def test_without_a_glossary_or_when_it_cannot_be_read_the_note_is_as_before(plugin, tmp_path):
    p, iface, dock, layers, _ = plugin
    assist = dock.note_assist
    assert not p.context.glossary.configured and assist.glossary.terms == ()
    assert dock.note_input.placeholderText() == ""
    dock.set_active(True)
    dock.map_clicked(139.7, 35.6, None)
    _type(dock.note_input, "入航")
    assert assist.shown == [] and not assist.notice.isVisible()
    # A table that cannot be read: said under the field, the words read before kept.
    fake, layer = _glossary(p, tmp_path)
    keeper = p.context.glossary
    del fake.layers[URL]
    assert keeper.read(force=True)
    assert wait_for(lambda: keeper.problem != "")
    assert keeper.problem.startswith("単語集を読めず、前に読んだ 4 語で候補を表示") and len(keeper.glossary) == 4
    dock.note_input.setText("")
    assist.check()
    # (The settings file read again closed the form: shown with it.)
    assert not assist.notice.isHidden() and assist.label.text() == keeper.problem and assist.fix_button.isHidden()
    # Read again (the classification's 「更新」 reads it too): the problem goes.
    fake.add_layer(URL, layer)
    dock.reload_taxonomy()
    assert wait_for(lambda: keeper.problem == "" and not keeper.reading)
    # Another settings file without it: no words.
    config = tmp_path / "config.json"
    config.write_text(json.dumps({}), encoding="utf-8")
    p.context.reload()
    assert not keeper.glossary and dock.note_input.placeholderText() == ""
